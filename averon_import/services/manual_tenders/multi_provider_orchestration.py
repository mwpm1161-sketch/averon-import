"""Explicit internal provider-only orchestration; no public/runtime activation."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated

from pydantic import Field, StrictInt, StrictStr

from averon_import.services.jobs import stable_fingerprint
from averon_import.services.sourcing.models import SourcingSourceMode
from averon_import.services.sourcing.providers.contracts import ProviderContractModel, ProviderSelection
from averon_import.services.sourcing.providers.execution import ProviderExecutionScope, ProviderRunnerConfigurationError
from averon_import.services.sourcing.service import SourcingService

from .durable_projection import (
    DurableProjectionCode, DurableProjectionError, DurableRowIdentity, DurableRunMetadata,
    build_durable_tender_sourcing_run_v2, project_durable_provider_row_v2,
)
from .durable_read import Fingerprint, Id, MAX_ACTUAL_ITEMS, DurableTenderSourcingRunV2
from .durable_storage import DurableV2StorageCode, DurableV2StorageError
from .repository import TenderWorkspaceRepository
from .sourcing import TenderSourcingRowAdapter, TenderSourcingRunStore


class MultiProviderOrchestrationCode(str, Enum):
    INVALID_INPUT = "MULTI_PROVIDER_INVALID_INPUT"
    UNSUPPORTED_MODE = "MULTI_PROVIDER_UNSUPPORTED_MODE"
    SNAPSHOT_CHANGED = "MULTI_PROVIDER_SNAPSHOT_CHANGED"
    INVALID_ROWS = "MULTI_PROVIDER_INVALID_ROWS"
    PROVIDER_CONFIGURATION = "MULTI_PROVIDER_CONFIGURATION"
    EVALUATION_FAILED = "MULTI_PROVIDER_EVALUATION_FAILED"
    INTERNAL_FAILED = "MULTI_PROVIDER_INTERNAL_FAILED"


class MultiProviderOrchestrationError(RuntimeError):
    def __init__(self, code: MultiProviderOrchestrationCode, run_id: str | None = None):
        self.code = code
        self.run_id = run_id
        super().__init__("Internal multi-provider run could not be produced.")


class MultiProviderTenderSnapshot(ProviderContractModel):
    """Capture with the server-only helper below; never a public request DTO."""

    workspace_path: Path
    tender_id: Id
    owner_id: Annotated[StrictStr, Field(min_length=1, max_length=1000)]
    source_sha256: Fingerprint
    workspace_revision: Annotated[StrictInt, Field(ge=1, le=1_000_000_000)]
    source_fingerprint: Fingerprint


@dataclass(frozen=True)
class MultiProviderTenderRunResult:
    run: DurableTenderSourcingRunV2
    failure_code: MultiProviderOrchestrationCode | None = None

    @property
    def run_id(self) -> str:
        return self.run.run_id

    @property
    def status(self) -> str:
        return self.run.status


def _source_fingerprint(metadata: dict) -> str:
    # Same server-owned source/manifest fence as legacy manual sourcing.
    return stable_fingerprint({key: metadata.get(key) for key in (
        "rows", "mapping", "source_manifest", "logical_right_edge",
        "future_output_columns", "counts", "sheet_name", "header_row",
    )})


def capture_multi_provider_tender_snapshot(
    repository: TenderWorkspaceRepository, tender_id: str, owner_id: str,
) -> MultiProviderTenderSnapshot:
    """Capture confirmed source facts before admission, without provider calls."""
    try:
        path, metadata = repository.workspace_path(tender_id, owner_id)
        metadata = repository.verify_sourcing_snapshot(
            path, tender_id, owner_id, source_sha256=metadata["source_sha256"], revision=metadata["revision"],
        )
        return MultiProviderTenderSnapshot(
            workspace_path=path.resolve(), tender_id=tender_id, owner_id=owner_id,
            source_sha256=metadata["source_sha256"], workspace_revision=metadata["revision"],
            source_fingerprint=_source_fingerprint(metadata),
        )
    except Exception:
        raise MultiProviderOrchestrationError(MultiProviderOrchestrationCode.SNAPSHOT_CHANGED) from None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("server clock must be timezone aware")
    return value.astimezone(timezone.utc)


def execute_multi_provider_tender_run(
    *, snapshot: MultiProviderTenderSnapshot, selection: ProviderSelection,
    selected_source_row_ids: tuple[str, ...], source_mode: SourcingSourceMode,
    service: SourcingService, store: TenderSourcingRunStore, limit: int = 20,
    progress: Callable[[int, int, str], None] | None = None,
    clock: Callable[[], datetime] = _now,
) -> MultiProviderTenderRunResult:
    """Run sequential exact row chains and persist exactly one terminal v2.

    Row evaluation exceptions may persist FAILED with previously projected rows.
    Capacity, build and storage errors never trigger a smaller fallback write.
    A failure before any complete row leaves no record. No cancellation/resume.
    """
    code = MultiProviderOrchestrationCode
    run_id = uuid.uuid4().hex
    lease = None
    try:
        if (type(snapshot) is not MultiProviderTenderSnapshot or type(selection) is not ProviderSelection
                or not isinstance(service, SourcingService) or not isinstance(store, TenderSourcingRunStore)
                or type(limit) is not int or not 1 <= limit <= 100):
            raise MultiProviderOrchestrationError(code.INVALID_INPUT, run_id)
        # Revalidate admission DTOs, not runtime decision phases.
        snapshot = MultiProviderTenderSnapshot.model_validate(snapshot.model_dump(mode="python"))
        selection = ProviderSelection.model_validate(selection.model_dump(mode="python"))
        if type(source_mode) is not SourcingSourceMode or source_mode != SourcingSourceMode.PROVIDER_ONLY:
            raise MultiProviderOrchestrationError(code.UNSUPPORTED_MODE, run_id)
        if (type(selected_source_row_ids) is not tuple or not 1 <= len(selected_source_row_ids) <= MAX_ACTUAL_ITEMS
                or any(type(item) is not str or not re.fullmatch(r"[a-f0-9]{32}", item) for item in selected_source_row_ids)
                or len(set(selected_source_row_ids)) != len(selected_source_row_ids)):
            raise MultiProviderOrchestrationError(code.INVALID_ROWS, run_id)
        if service.provider_runner is None or not set(selection.provider_keys).issubset({"etm_ipro", "lemana_b2b", "local_catalog"}):
            raise MultiProviderOrchestrationError(code.PROVIDER_CONFIGURATION, run_id)
        started = _timestamp(clock)
        repository = store.repository
        try:
            path, _, lease = repository.acquire_sourcing_workspace(snapshot.tender_id, snapshot.owner_id)
            if path.resolve() != snapshot.workspace_path.resolve():
                raise ValueError("workspace path differs")
            metadata = repository.verify_sourcing_snapshot(
                path, snapshot.tender_id, snapshot.owner_id,
                source_sha256=snapshot.source_sha256, revision=snapshot.workspace_revision,
            )
            if _source_fingerprint(metadata) != snapshot.source_fingerprint:
                raise ValueError("source facts differ")
        except Exception:
            raise MultiProviderOrchestrationError(code.SNAPSHOT_CHANGED, run_id) from None
        try:
            source_rows = metadata["rows"]
            # Existing adapter validates the complete row-ID index and item scope.
            adapted = TenderSourcingRowAdapter.selected_rows(source_rows, list(selected_source_row_ids))
            identities = {row["source_row_id"]: DurableRowIdentity(
                source_row_id=row["source_row_id"], physical_excel_row=row["excel_row"],
            ) for row in source_rows if row["source_row_id"] in selected_source_row_ids}
            adapted.sort(key=lambda row: identities[row["source_row_id"]].physical_excel_row)
        except Exception:
            raise MultiProviderOrchestrationError(code.INVALID_ROWS, run_id) from None

        # A new scope per invocation: dedup is shared only within this tender run.
        scope = ProviderExecutionScope(execution_scope_id=run_id)
        rows = []
        failure_code = None
        for source in adapted:
            try:
                intent = service.understand_row(source)
                execution = service.execute_provider_selection(
                    intent, selection=selection, limit=limit, execution_scope=scope,
                )
                matching = service.evaluate_provider_execution(intent, execution)
                commercial = service.evaluate_provider_commercial_evidence(matching)
                winner = service.select_provider_commercial_winner(commercial)
                rows.append(project_durable_provider_row_v2(
                    identities[source["source_row_id"]], execution=execution,
                    matching=matching, commercial=commercial, selection=winner,
                ))
                if progress is not None:
                    try:
                        progress(len(rows), len(adapted), "Обработано позиций")
                    except Exception:
                        # Progress is observational; it must never change run results.
                        pass
            except ProviderRunnerConfigurationError:
                raise MultiProviderOrchestrationError(code.PROVIDER_CONFIGURATION, run_id) from None
            except DurableProjectionError as exc:
                if exc.code == DurableProjectionCode.TOO_LARGE:
                    raise DurableProjectionError(exc.code) from None
                failure_code = code.EVALUATION_FAILED
                break
            except Exception:
                failure_code = code.EVALUATION_FAILED
                break
        if failure_code is not None and not rows:
            raise MultiProviderOrchestrationError(failure_code, run_id)
        completed = _timestamp(clock)
        if completed < started:
            raise MultiProviderOrchestrationError(code.INTERNAL_FAILED, run_id)
        run = build_durable_tender_sourcing_run_v2(DurableRunMetadata(
            run_id=run_id, tender_id=snapshot.tender_id, source_sha256=snapshot.source_sha256,
            workspace_revision=snapshot.workspace_revision, status="failed" if failure_code else "completed",
            created_at=started.isoformat(), started_at=started.isoformat(), completed_at=completed.isoformat(),
            selected_source_row_ids=selected_source_row_ids,
        ), selection, tuple(rows))
        try:
            store.persist_durable_v2(path, run)
        except DurableV2StorageError as exc:
            raise DurableV2StorageError(exc.code) from None
        except Exception:
            raise DurableV2StorageError(DurableV2StorageCode.STORAGE_FAILED) from None
        return MultiProviderTenderRunResult(run=run, failure_code=failure_code)
    except (MultiProviderOrchestrationError, DurableV2StorageError, DurableProjectionError):
        raise
    except Exception:
        raise MultiProviderOrchestrationError(code.INTERNAL_FAILED, run_id) from None
    finally:
        if lease is not None:
            lease.release()
