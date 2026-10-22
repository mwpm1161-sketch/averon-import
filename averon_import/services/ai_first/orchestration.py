"""Model-agnostic bounded page orchestration for the isolated AI-first foundation.

This module is deliberately not wired into production. Checkpoints contain raw model
observations and must be placed outside the repository in a private local directory.
"""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Any, Awaitable, Callable, Literal, Protocol
from uuid import uuid4

import fitz
from pydantic import BaseModel, ConfigDict, Field, model_validator

from averon_import.services.ai_first.assembly import assemble_document_draft, failed_page
from averon_import.services.ai_first.contracts import AIDocumentDraft, AIPageExtraction


class OrchestrationError(RuntimeError):
    """Safe error code; never includes document, image, model, or provider content."""

    def __init__(self, code: str):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            code = "orchestration_error"
        super().__init__(code)
        self.code = code


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


class AdapterDescriptor(_StrictModel):
    provider_id: str
    model_id: str
    model_version: str
    prompt_version: str
    schema_version: str
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def safe_descriptor(self) -> AdapterDescriptor:
        names = (self.provider_id, self.model_id, self.model_version, self.prompt_version, self.schema_version)
        if any(not value or len(value) > 160 for value in names):
            raise ValueError("invalid adapter descriptor")
        forbidden = re.compile(r"(?:api[_-]?key|secret|credential|authorization|password|access[_-]?token|refresh[_-]?token)", re.I)
        if any(forbidden.search(key) for key in self.parameters):
            raise ValueError("secret-like adapter parameter name")
        try:
            _canonical_bytes(self.parameters)
        except (TypeError, ValueError):
            raise ValueError("adapter parameters must be JSON-safe") from None
        return self

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json"))


class TokenUsage(_StrictModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def compatible_usage(self) -> TokenUsage:
        if self.cached_input_tokens is not None and self.input_tokens is not None:
            if self.cached_input_tokens > self.input_tokens:
                raise ValueError("cached input exceeds input usage")
        if self.reasoning_tokens is not None and self.output_tokens is not None:
            if self.reasoning_tokens > self.output_tokens:
                raise ValueError("reasoning usage exceeds output usage")
        return self


class TokenPricing(_StrictModel):
    input_rub_per_1k: Decimal = Field(gt=0)
    cached_input_rub_per_1k: Decimal | None = Field(default=None, gt=0)
    output_rub_per_1k: Decimal = Field(gt=0)

    def reserve_cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        # Reserve all input at the uncached rate. Cached pricing is a later credit.
        return (
            Decimal(input_tokens) * self.input_rub_per_1k
            + Decimal(output_tokens) * self.output_rub_per_1k
        ) / Decimal(1000)

    def actual_cost(self, usage: TokenUsage) -> Decimal | None:
        if usage.input_tokens is None or usage.output_tokens is None:
            return None
        cached = usage.cached_input_tokens or 0
        cached_rate = self.cached_input_rub_per_1k or self.input_rub_per_1k
        input_cost = Decimal(usage.input_tokens - cached) * self.input_rub_per_1k
        input_cost += Decimal(cached) * cached_rate
        return (
            input_cost + Decimal(usage.output_tokens) * self.output_rub_per_1k
        ) / Decimal(1000)


class OrchestrationConfig(_StrictModel):
    concurrency: int = Field(default=3, ge=1, le=8)
    dpi: int = Field(default=150, ge=36, le=600)
    requests_per_minute: int = Field(default=60, ge=1, le=10000)
    tokens_per_minute: int = Field(default=120000, ge=1, le=100000000)
    max_budget_rub: Decimal = Field(gt=0)
    pricing: TokenPricing
    max_image_bytes: int = Field(default=20_000_000, ge=1, le=100_000_000)
    max_image_pixels: int = Field(default=30_000_000, ge=1, le=100_000_000)
    max_pages: int = Field(default=1000, ge=1, le=10000)

    def processing_fingerprint(self) -> str:
        # Concurrency, RPM, TPM, and the budget cap may be adjusted for resume.
        # They do not alter the image or model extraction content.
        return _fingerprint({
            "dpi": self.dpi,
            "max_image_bytes": self.max_image_bytes,
            "max_image_pixels": self.max_image_pixels,
            "pricing": self.pricing.model_dump(mode="json"),
        })


class CheckpointIdentity(_StrictModel):
    source_sha256: str
    page_count: int = Field(ge=1)
    run_id: str
    source_document_id: str
    adapter_fingerprint: str
    processing_fingerprint: str

    @model_validator(mode="after")
    def valid_identity(self) -> CheckpointIdentity:
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_sha256):
            raise ValueError("invalid source digest")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,96}", self.run_id):
            raise ValueError("invalid run id")
        if self.source_document_id != f"pdf-sha256:{self.source_sha256}":
            raise ValueError("source id mismatch")
        return self

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json"))


class PageImage:
    """Ephemeral single-page image. Never serialized into checkpoints or logs."""

    __slots__ = ("page_number", "data", "content_type", "width", "height", "dpi")

    def __init__(self, page_number: int, data: bytes, content_type: str, width: int, height: int, dpi: int):
        if page_number < 1 or not data or width < 1 or height < 1 or dpi < 1:
            raise OrchestrationError("invalid_page_image")
        self.page_number = page_number
        self.data = data
        self.content_type = content_type
        self.width = width
        self.height = height
        self.dpi = dpi


class RequestEstimate(_StrictModel):
    """Conservative pre-send reservation, not a point estimate."""
    reserved_input_tokens: int = Field(ge=0)
    reserved_output_tokens: int = Field(gt=0)


class PageExtractionRequest:
    __slots__ = ("source_document_id", "page_number", "image", "run_id")

    def __init__(self, source_document_id: str, page_number: int, image: PageImage, run_id: str):
        self.source_document_id = source_document_id
        self.page_number = page_number
        self.image = image
        self.run_id = run_id


class AdapterPageResult(_StrictModel):
    extraction: AIPageExtraction
    usage: TokenUsage | None = None


class PageExtractionAdapter(Protocol):
    @property
    def descriptor(self) -> AdapterDescriptor: ...

    def estimate(self, image: PageImage) -> RequestEstimate: ...

    async def extract(self, request: PageExtractionRequest) -> AdapterPageResult: ...


class AdapterRequestError(RuntimeError):
    """Adapter failure with an explicit safe code and dispatch ambiguity."""

    def __init__(
        self, safe_code: str, *, outcome_unknown: bool, usage: TokenUsage | None = None,
    ):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", safe_code):
            safe_code = "adapter_error"
        super().__init__(safe_code)
        self.safe_code = safe_code
        self.outcome_unknown = outcome_unknown
        self.usage = usage


class PageAttempt(_StrictModel):
    state: Literal["running", "completed", "failed", "unknown_outcome"]
    started_at: str
    finished_at: str | None = None
    request_started: bool = False
    reserved_input_tokens: int | None = Field(default=None, ge=0)
    reserved_output_tokens: int | None = Field(default=None, ge=0)
    reserved_cost_rub: Decimal | None = Field(default=None, ge=0)
    actual_cost_rub: Decimal | None = Field(default=None, ge=0)
    usage: TokenUsage | None = None
    safe_error_code: str | None = None
    elapsed_s: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def attempt_consistency(self) -> PageAttempt:
        if self.state == "running" and self.finished_at is not None:
            raise ValueError("running attempt cannot have finish time")
        if self.state != "running" and self.finished_at is None:
            raise ValueError("finished attempt requires finish time")
        if self.state in ("failed", "unknown_outcome") and not self.safe_error_code:
            raise ValueError("failed attempt requires safe error code")
        return self


class PageRunRecord(_StrictModel):
    page_number: int = Field(ge=1)
    state: Literal["pending", "running", "completed", "failed", "unknown_outcome"]
    attempts: list[PageAttempt] = Field(default_factory=list)
    extraction: AIPageExtraction | None = None
    provider_id: str | None = None
    model_id: str | None = None
    model_version: str | None = None
    prompt_version: str | None = None
    schema_version: str | None = None
    configuration_fingerprint: str | None = None
    updated_at: str

    @model_validator(mode="after")
    def record_consistency(self) -> PageRunRecord:
        if self.state == "completed" and self.extraction is None:
            raise ValueError("completed page requires extraction")
        if self.extraction is not None and self.extraction.page_number != self.page_number:
            raise ValueError("record page mismatch")
        if self.state == "running" and (not self.attempts or self.attempts[-1].state != "running"):
            raise ValueError("running record requires running attempt")
        return self


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def inspect_pdf(
    pdf_path: str | os.PathLike[str],
    adapter: PageExtractionAdapter,
    config: OrchestrationConfig,
    *,
    run_id: str | None = None,
) -> CheckpointIdentity:
    """Hash the source and read its local page count before any model request."""
    path = Path(pdf_path)
    if not path.is_file():
        raise OrchestrationError("source_pdf_unavailable")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        with fitz.open(path) as document:
            count = document.page_count
    except Exception:
        raise OrchestrationError("source_pdf_invalid") from None
    if count < 1 or count > config.max_pages:
        raise OrchestrationError("source_page_count_out_of_range")
    source_sha256 = digest.hexdigest()
    return CheckpointIdentity(
        source_sha256=source_sha256,
        page_count=count,
        run_id=run_id or uuid4().hex,
        source_document_id=f"pdf-sha256:{source_sha256}",
        adapter_fingerprint=adapter.descriptor.fingerprint,
        processing_fingerprint=config.processing_fingerprint(),
    )


class FitZPageRenderer:
    """Renders only the requested page and releases its PDF handle immediately."""

    def render(self, pdf_path: str | os.PathLike[str], page_number: int, dpi: int, config: OrchestrationConfig) -> PageImage:
        try:
            with fitz.open(pdf_path) as document:
                if page_number < 1 or page_number > document.page_count:
                    raise OrchestrationError("source_page_out_of_range")
                page = document.load_page(page_number - 1)
                pixmap = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False)
                if pixmap.width * pixmap.height > config.max_image_pixels:
                    raise OrchestrationError("image_pixel_limit")
                data = pixmap.tobytes("png")
                width, height = pixmap.width, pixmap.height
        except OrchestrationError:
            raise
        except Exception:
            raise OrchestrationError("page_render_failed") from None
        if len(data) > config.max_image_bytes:
            raise OrchestrationError("image_byte_limit")
        return PageImage(page_number, data, "image/png", width, height, dpi)


class CheckpointStore:
    """Private, atomic per-page JSON checkpoint with an OS-released run lock."""

    def __init__(self, directory: str | os.PathLike[str], identity: CheckpointIdentity):
        self.directory = Path(directory).expanduser().resolve()
        self.identity = identity
        self._lock_stream: Any = None
        self._io_lock = asyncio.Lock()

    def open(self) -> dict[int, PageRunRecord]:
        repo_root = Path(__file__).resolve().parents[3]
        try:
            self.directory.relative_to(repo_root)
        except ValueError:
            pass
        else:
            raise OrchestrationError("checkpoint_must_be_outside_repository")
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._secure_tree()
            self._acquire_process_lock()
        except OrchestrationError:
            raise
        except Exception:
            self.close()
            raise OrchestrationError("checkpoint_unavailable") from None

        manifest = self.directory / "manifest.json"
        if manifest.is_symlink():
            self.close()
            raise OrchestrationError("checkpoint_invalid")
        expected = {
            "format_version": 1,
            "identity": self.identity.model_dump(mode="json"),
            "identity_fingerprint": self.identity.fingerprint,
        }
        try:
            if manifest.exists():
                actual = json.loads(manifest.read_text(encoding="utf-8"))
                if actual != expected:
                    raise OrchestrationError("checkpoint_identity_mismatch")
            else:
                if any(path.name != ".run.lock" for path in self.directory.iterdir()):
                    raise OrchestrationError("checkpoint_manifest_missing")
                self._atomic_json(manifest, expected)
            records: dict[int, PageRunRecord] = {}
            for path in self.directory.glob("page-*.json"):
                if path.is_symlink():
                    raise OrchestrationError("checkpoint_invalid")
                record = PageRunRecord.model_validate_json(path.read_text(encoding="utf-8"))
                if path.name != f"page-{record.page_number:06d}.json" or record.page_number > self.identity.page_count:
                    raise OrchestrationError("checkpoint_invalid")
                if record.extraction is not None and record.extraction.source_document_id != self.identity.source_document_id:
                    raise OrchestrationError("checkpoint_invalid")
                if record.state == "running":
                    attempt = record.attempts[-1].model_copy(update={
                        "state": "unknown_outcome",
                        "finished_at": _utc_now(),
                        "safe_error_code": "process_restarted_during_request",
                    })
                    record = record.model_copy(update={
                        "state": "unknown_outcome",
                        "attempts": [*record.attempts[:-1], attempt],
                        "updated_at": _utc_now(),
                    })
                    self._atomic_json(path, record.model_dump(mode="json"))
                records[record.page_number] = record
            return records
        except OrchestrationError:
            self.close()
            raise
        except Exception:
            self.close()
            raise OrchestrationError("checkpoint_invalid") from None

    def _secure_tree(self) -> None:
        if os.name == "nt":
            try:
                sid_result = subprocess.run(
                    ["whoami.exe", "/user", "/fo", "csv", "/nh"],
                    check=True, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", stdin=subprocess.DEVNULL, timeout=10,
                )
                import csv
                rows = list(csv.reader(sid_result.stdout.splitlines()))
                sid = rows[0][-1].strip() if rows else ""
                if not re.fullmatch(r"S-\d-(?:\d+-)*\d+", sid):
                    raise OSError
                subprocess.run(
                    ["icacls.exe", str(self.directory), "/inheritance:r", "/grant:r",
                     f"*{sid}:F", "*S-1-5-18:F", "*S-1-5-32-544:F", "/T", "/Q"],
                    check=True, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", stdin=subprocess.DEVNULL, timeout=20,
                )
                subprocess.run(
                    ["icacls.exe", str(self.directory), "/grant",
                     f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F",
                     "*S-1-5-32-544:(OI)(CI)F", "/Q"],
                    check=True, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", stdin=subprocess.DEVNULL, timeout=20,
                )
            except Exception:
                raise OrchestrationError("checkpoint_permissions_failed") from None
        else:
            try:
                for root, dirs, files in os.walk(self.directory, followlinks=False):
                    root_path = Path(root)
                    if root_path.is_symlink() or any((root_path / name).is_symlink() for name in files + dirs):
                        raise OSError
                    os.chmod(root_path, 0o700)
                    for name in files:
                        os.chmod(root_path / name, 0o600)
                if self.directory.stat().st_mode & 0o077:
                    raise OSError
            except Exception:
                raise OrchestrationError("checkpoint_permissions_failed") from None

    def _acquire_process_lock(self) -> None:
        lock_path = self.directory / ".run.lock"
        self._lock_stream = lock_path.open("a+b")
        try:
            if os.name != "nt":
                os.chmod(lock_path, 0o600)
            self._lock_stream.seek(0, os.SEEK_END)
            if self._lock_stream.tell() == 0:
                self._lock_stream.write(b"\0")
                self._lock_stream.flush()
            self._lock_stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._lock_stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            self.close()
            raise OrchestrationError("checkpoint_in_use") from None

    def _atomic_json(self, path: Path, payload: Any) -> None:
        temp_name: str | None = None
        try:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8")
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{path.name}.", suffix=".tmp",
                dir=self.directory, delete=False,
            ) as stream:
                temp_name = stream.name
                if os.name != "nt":
                    os.chmod(temp_name, 0o600)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
            temp_name = None
            if os.name != "nt":
                descriptor = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except Exception:
            raise OrchestrationError("checkpoint_write_failed") from None
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass

    async def write(self, record: PageRunRecord) -> None:
        async with self._io_lock:
            path = self.directory / f"page-{record.page_number:06d}.json"
            if path.is_symlink():
                raise OrchestrationError("checkpoint_invalid")
            self._atomic_json(path, record.model_dump(mode="json"))

    def close(self) -> None:
        if self._lock_stream is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    self._lock_stream.seek(0)
                    msvcrt.locking(self._lock_stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._lock_stream.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self._lock_stream.close()
            finally:
                self._lock_stream = None


class _RateGate:
    def __init__(self, rpm: int, tpm: int, *, window_s: float = 60.0):
        self.rpm = rpm
        self.tpm = tpm
        self.window_s = window_s
        self.events: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()

    async def acquire(
        self, token_count: int, cancel_event: asyncio.Event,
        stop_event: asyncio.Event | None = None,
    ) -> bool:
        if token_count > self.tpm:
            return False
        loop = asyncio.get_running_loop()
        while True:
            if cancel_event.is_set() or (stop_event is not None and stop_event.is_set()):
                return False
            async with self._lock:
                now = loop.time()
                while self.events and self.events[0][0] <= now - self.window_s:
                    self.events.popleft()
                used_tokens = sum(value for _, value in self.events)
                if len(self.events) < self.rpm and used_tokens + token_count <= self.tpm:
                    self.events.append((now, token_count))
                    return True
                if not self.events:
                    return False
                delay = max(0.001, self.events[0][0] + self.window_s - now)
            waiters = [asyncio.create_task(cancel_event.wait())]
            if stop_event is not None:
                waiters.append(asyncio.create_task(stop_event.wait()))
            done, pending = await asyncio.wait(
                waiters, timeout=delay, return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
            if done:
                return False


class _BudgetLedger:
    def __init__(self, cap: Decimal, records: dict[int, PageRunRecord]):
        self.cap = cap
        self.spent = Decimal(0)
        self.reserved = Decimal(0)
        self._lock = asyncio.Lock()
        for record in records.values():
            for attempt in record.attempts:
                if attempt.actual_cost_rub is not None:
                    self.spent += attempt.actual_cost_rub
                elif attempt.request_started and attempt.reserved_cost_rub is not None:
                    # Unknown usage remains reserved after process restart.
                    self.reserved += attempt.reserved_cost_rub

    async def reserve(self, amount: Decimal) -> bool:
        async with self._lock:
            if self.spent + self.reserved + amount > self.cap:
                return False
            self.reserved += amount
            return True

    async def settle(self, reserved: Decimal, actual: Decimal | None, *, request_started: bool) -> bool:
        async with self._lock:
            self.reserved -= reserved
            if actual is not None:
                self.spent += actual
            elif request_started:
                self.reserved += reserved
            return self.spent + self.reserved > self.cap


class ProgressEvent(_StrictModel):
    page_number: int
    state: Literal["pending", "running", "completed", "failed", "unknown_outcome"]
    finished_pages: int = Field(ge=0)
    total_pages: int = Field(ge=1)


class OrchestrationReport(_StrictModel):
    status: Literal["completed", "completed_with_errors", "cancelled", "budget_exhausted"]
    run_id: str
    adapter: AdapterDescriptor
    config: OrchestrationConfig
    source_document_id: str
    source_sha256: str
    expected_page_count: int = Field(ge=1)
    pages: list[PageRunRecord]
    draft: AIDocumentDraft
    elapsed_s: float = Field(ge=0)
    max_parallel_requests: int = Field(ge=0)
    max_inflight_image_count: int = Field(ge=0)
    max_inflight_image_bytes: int = Field(ge=0)
    actual_cost_rub: Decimal | None = Field(default=None, ge=0)
    reserved_unknown_cost_rub: Decimal = Field(ge=0)
    actual_input_tokens: int | None = Field(default=None, ge=0)
    actual_output_tokens: int | None = Field(default=None, ge=0)
    actual_reasoning_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def ordered_complete_manifest(self) -> OrchestrationReport:
        if [record.page_number for record in self.pages] != list(range(1, self.expected_page_count + 1)):
            raise ValueError("report must include all source pages in order")
        if self.draft.expected_page_numbers != list(range(1, self.expected_page_count + 1)):
            raise ValueError("draft must use PDF page manifest")
        if self.draft.export_authorized is not False or self.draft.extraction_is_verified is not False:
            raise ValueError("orchestration cannot authorize extraction")
        return self


ProgressCallback = Callable[[ProgressEvent], None | Awaitable[None]]


class PageOrchestrator:
    def __init__(
        self,
        adapter: PageExtractionAdapter,
        renderer: Any,
        config: OrchestrationConfig,
        checkpoint: CheckpointStore,
        *,
        logger: logging.Logger | None = None,
        progress: ProgressCallback | None = None,
        rate_window_s: float = 60.0,
    ):
        self.adapter = adapter
        self.renderer = renderer
        self.config = config
        self.checkpoint = checkpoint
        self.logger = logger or logging.getLogger(__name__)
        self.progress = progress
        self.rate_window_s = rate_window_s
        self._active_requests = 0
        self._max_active_requests = 0
        self._active_images: dict[int, int] = {}
        self._max_images = 0
        self._max_image_bytes = 0

    async def run(
        self,
        pdf_path: str | os.PathLike[str],
        *,
        cancel_event: asyncio.Event | None = None,
        retry_pages: set[int] | None = None,
    ) -> OrchestrationReport:
        started = time.perf_counter()
        cancel_event = cancel_event or asyncio.Event()
        identity = self.checkpoint.identity
        if identity.adapter_fingerprint != self.adapter.descriptor.fingerprint:
            raise OrchestrationError("adapter_fingerprint_mismatch")
        if identity.processing_fingerprint != self.config.processing_fingerprint():
            raise OrchestrationError("processing_fingerprint_mismatch")
        actual_identity = inspect_pdf(pdf_path, self.adapter, self.config, run_id=identity.run_id)
        if actual_identity != identity:
            raise OrchestrationError("source_or_configuration_identity_mismatch")
        records = self.checkpoint.open()
        total = identity.page_count
        retry_pages = set(retry_pages or ())
        if any(type(page) is not int or page < 1 or page > total for page in retry_pages):
            self.checkpoint.close()
            raise OrchestrationError("invalid_retry_page")
        for page_number in retry_pages:
            current = records.get(page_number)
            if current is None or current.state not in ("failed", "unknown_outcome"):
                self.checkpoint.close()
                raise OrchestrationError("retry_requires_failed_page")
            reset = current.model_copy(update={"state": "pending", "updated_at": _utc_now()})
            records[page_number] = reset
            await self.checkpoint.write(reset)

        pending = []
        for number in range(1, total + 1):
            record = records.get(number)
            if record is None:
                record = PageRunRecord(page_number=number, state="pending", updated_at=_utc_now())
                records[number] = record
            if record.state == "pending":
                pending.append(number)

        rate_gate = _RateGate(
            self.config.requests_per_minute, self.config.tokens_per_minute,
            window_s=self.rate_window_s,
        )
        budget = _BudgetLedger(self.config.max_budget_rub, records)
        queue: asyncio.Queue[int | None] = asyncio.Queue(maxsize=max(1, self.config.concurrency * 2))
        stop_scheduling = asyncio.Event()
        finished = sum(1 for record in records.values() if record.state in ("completed", "failed", "unknown_outcome"))

        async def notify(page_number: int, state: str) -> None:
            nonlocal finished
            if state in ("completed", "failed", "unknown_outcome"):
                finished += 1
            self.logger.info("ai_first_page_state", extra={
                "page_number": page_number, "state": state, "finished_pages": finished, "total_pages": total,
            })
            if self.progress is not None:
                event = ProgressEvent(
                    page_number=page_number, state=state, finished_pages=finished, total_pages=total,
                )
                try:
                    result = self.progress(event)
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    self.logger.warning("ai_first_progress_callback_failed", extra={"page_number": page_number})

        async def produce() -> None:
            for number in pending:
                if cancel_event.is_set() or stop_scheduling.is_set():
                    break
                await queue.put(number)
            for _ in range(self.config.concurrency):
                await queue.put(None)

        async def process_page(number: int) -> None:
            current = records[number]
            try:
                image = await asyncio.to_thread(
                    self.renderer.render, pdf_path, number, self.config.dpi, self.config,
                )
            except OrchestrationError as exc:
                records[number] = current.model_copy(update={
                    "state": "failed", "updated_at": _utc_now(),
                    "attempts": [*current.attempts, PageAttempt(
                        state="failed", started_at=_utc_now(), finished_at=_utc_now(),
                        safe_error_code=exc.code, elapsed_s=0.0,
                    )],
                })
                await self.checkpoint.write(records[number])
                await notify(number, "failed")
                return
            except Exception:
                records[number] = current.model_copy(update={
                    "state": "failed", "updated_at": _utc_now(),
                    "attempts": [*current.attempts, PageAttempt(
                        state="failed", started_at=_utc_now(), finished_at=_utc_now(),
                        safe_error_code="page_render_failed", elapsed_s=0.0,
                    )],
                })
                await self.checkpoint.write(records[number])
                await notify(number, "failed")
                return
            if cancel_event.is_set() or stop_scheduling.is_set():
                return
            if not isinstance(image, PageImage) or image.page_number != number:
                records[number] = current.model_copy(update={
                    "state": "failed", "updated_at": _utc_now(),
                    "attempts": [*current.attempts, PageAttempt(
                        state="failed", started_at=_utc_now(), finished_at=_utc_now(),
                        safe_error_code="renderer_page_identity_mismatch", elapsed_s=0.0,
                    )],
                })
                await self.checkpoint.write(records[number])
                await notify(number, "failed")
                return
            self._active_images[number] = len(image.data)
            self._max_images = max(self._max_images, len(self._active_images))
            self._max_image_bytes = max(self._max_image_bytes, sum(self._active_images.values()))
            request_started_at = time.perf_counter()
            try:
                estimate = self.adapter.estimate(image)
                if estimate.reserved_input_tokens + estimate.reserved_output_tokens > self.config.tokens_per_minute:
                    raise OrchestrationError("request_exceeds_token_rate_limit")
            except OrchestrationError as exc:
                self._active_images.pop(number, None)
                records[number] = current.model_copy(update={
                    "state": "failed", "updated_at": _utc_now(),
                    "attempts": [*current.attempts, PageAttempt(
                        state="failed", started_at=_utc_now(), finished_at=_utc_now(),
                        safe_error_code=exc.code, elapsed_s=time.perf_counter() - request_started_at,
                    )],
                })
                await self.checkpoint.write(records[number])
                await notify(number, "failed")
                return
            except Exception:
                self._active_images.pop(number, None)
                records[number] = current.model_copy(update={
                    "state": "failed", "updated_at": _utc_now(),
                    "attempts": [*current.attempts, PageAttempt(
                        state="failed", started_at=_utc_now(), finished_at=_utc_now(),
                        safe_error_code="request_estimate_invalid", elapsed_s=time.perf_counter() - request_started_at,
                    )],
                })
                await self.checkpoint.write(records[number])
                await notify(number, "failed")
                return

            if stop_scheduling.is_set():
                self._active_images.pop(number, None)
                return

            reserve = self.config.pricing.reserve_cost(estimate.reserved_input_tokens, estimate.reserved_output_tokens)
            if not await budget.reserve(reserve):
                self._active_images.pop(number, None)
                stop_scheduling.set()
                return

            attempt = PageAttempt(
                state="running", started_at=_utc_now(),
                request_started=True,
                reserved_input_tokens=estimate.reserved_input_tokens,
                reserved_output_tokens=estimate.reserved_output_tokens,
                reserved_cost_rub=reserve,
            )
            running = current.model_copy(update={
                "state": "running", "attempts": [*current.attempts, attempt], "updated_at": _utc_now(),
            })
            records[number] = running
            await self.checkpoint.write(running)
            await notify(number, "running")
            async def stop_before_dispatch(code: str) -> None:
                latest = running.attempts[-1].model_copy(update={
                    "state": "failed", "request_started": False,
                    "finished_at": _utc_now(), "safe_error_code": code,
                    "elapsed_s": time.perf_counter() - request_started_at,
                })
                await budget.settle(reserve, None, request_started=False)
                records[number] = running.model_copy(update={
                    "state": "pending", "attempts": [*running.attempts[:-1], latest],
                    "updated_at": _utc_now(),
                })
                self._active_images.pop(number, None)
                await self.checkpoint.write(records[number])
                await notify(number, "pending")

            if cancel_event.is_set():
                await stop_before_dispatch("cancelled_before_dispatch")
                return
            if not await rate_gate.acquire(estimate.reserved_input_tokens + estimate.reserved_output_tokens, cancel_event, stop_scheduling):
                code = "cancelled_before_dispatch" if cancel_event.is_set() else "budget_stopped_before_dispatch"
                await stop_before_dispatch(code)
                return
            if cancel_event.is_set():
                await stop_before_dispatch("cancelled_before_dispatch")
                return
            if stop_scheduling.is_set():
                await stop_before_dispatch("budget_stopped_before_dispatch")
                return

            response: AdapterPageResult | None = None
            error_state: str | None = None
            error_code: str | None = None
            error_usage: TokenUsage | None = None
            request_task = asyncio.create_task(self.adapter.extract(PageExtractionRequest(
                identity.source_document_id, number, image, identity.run_id,
            )))
            cancel_task = asyncio.create_task(cancel_event.wait())
            self._active_requests += 1
            self._max_active_requests = max(self._max_active_requests, self._active_requests)
            try:
                done, _ = await asyncio.wait(
                    {request_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED,
                )
                if request_task in done:
                    cancel_task.cancel()
                    await asyncio.gather(cancel_task, return_exceptions=True)
                    response = await request_task
                else:
                    request_task.cancel()
                    await asyncio.gather(request_task, return_exceptions=True)
                    error_state, error_code = "unknown_outcome", "cancelled_after_dispatch"
            except AdapterRequestError as exc:
                error_state = "unknown_outcome" if exc.outcome_unknown else "failed"
                error_code = exc.safe_code
                error_usage = exc.usage
            except asyncio.CancelledError:
                request_task.cancel()
                cancel_task.cancel()
                await asyncio.gather(request_task, cancel_task, return_exceptions=True)
                error_state, error_code = "unknown_outcome", "cancelled_after_dispatch"
            except Exception:
                error_state, error_code = "unknown_outcome", "adapter_outcome_unknown"
            finally:
                self._active_requests -= 1
                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)
                self._active_images.pop(number, None)

            usage: TokenUsage | None = error_usage
            actual_cost: Decimal | None = self.config.pricing.actual_cost(usage) if usage is not None else None
            if response is not None and not isinstance(response, AdapterPageResult):
                error_state, error_code = "failed", "adapter_response_invalid"
                response = None
            if response is not None:
                usage = response.usage
                actual_cost = self.config.pricing.actual_cost(usage) if usage is not None else None
                extraction = response.extraction
                if extraction.source_document_id != identity.source_document_id or extraction.page_number != number:
                    error_state, error_code = "failed", "response_identity_mismatch"
                    response = None
                elif extraction.status == "failed":
                    candidate_code = extraction.error_code or "page_extraction_failed"
                    error_code = candidate_code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", candidate_code) else "page_extraction_failed"
                    error_state = "failed"
                    if error_code != extraction.error_code:
                        safe_extraction = extraction.model_copy(update={"error_code": error_code})
                        response = response.model_copy(update={"extraction": safe_extraction})

            old_attempts = running.attempts
            final_state = error_state or "completed"
            updated_attempt = old_attempts[-1].model_copy(update={
                "state": final_state,
                "finished_at": _utc_now(),
                "actual_cost_rub": actual_cost,
                "usage": usage,
                "safe_error_code": error_code,
                "elapsed_s": time.perf_counter() - request_started_at,
            })
            if await budget.settle(reserve, actual_cost, request_started=True):
                stop_scheduling.set()
            extraction_value = response.extraction if response is not None else running.extraction
            descriptor = self.adapter.descriptor
            records[number] = running.model_copy(update={
                "state": final_state,
                "attempts": [*old_attempts[:-1], updated_attempt],
                "extraction": extraction_value,
                "provider_id": descriptor.provider_id,
                "model_id": descriptor.model_id,
                "model_version": descriptor.model_version,
                "prompt_version": descriptor.prompt_version,
                "schema_version": descriptor.schema_version,
                "configuration_fingerprint": identity.adapter_fingerprint,
                "updated_at": _utc_now(),
            })
            await self.checkpoint.write(records[number])
            await notify(number, final_state)

        async def worker() -> None:
            while not cancel_event.is_set() and not stop_scheduling.is_set():
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                if item is None:
                    return
                if cancel_event.is_set() or stop_scheduling.is_set():
                    return
                await process_page(item)

        producer = asyncio.create_task(produce())
        workers = [asyncio.create_task(worker()) for _ in range(self.config.concurrency)]
        try:
            await asyncio.gather(*workers)
        except BaseException:
            for task in workers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
            # Running attempts left by cancellation are ambiguous and must not be retried.
            for number, record in list(records.items()):
                if record.state == "running":
                    latest = record.attempts[-1].model_copy(update={
                        "state": "unknown_outcome", "finished_at": _utc_now(),
                        "safe_error_code": "orchestrator_interrupted",
                    })
                    records[number] = record.model_copy(update={
                        "state": "unknown_outcome", "attempts": [*record.attempts[:-1], latest],
                        "updated_at": _utc_now(),
                    })
                if record.state == "pending" or records[number].state == "unknown_outcome":
                    await self.checkpoint.write(records[number])
            self.checkpoint.close()

        for number in range(1, total + 1):
            if number not in records:
                records[number] = PageRunRecord(page_number=number, state="pending", updated_at=_utc_now())
        ordered_records = [records[n] for n in range(1, total + 1)]
        draft_pages: list[AIPageExtraction] = []
        for record in ordered_records:
            if record.extraction is not None:
                draft_pages.append(record.extraction)
            else:
                code = record.attempts[-1].safe_error_code if record.attempts else None
                if code is None and stop_scheduling.is_set():
                    code = "budget_exhausted_before_dispatch"
                elif code is None and cancel_event.is_set():
                    code = "cancelled_before_dispatch"
                else:
                    code = "page_not_processed"
                draft_pages.append(failed_page(identity.source_document_id, record.page_number, code))
        draft = assemble_document_draft(
            identity.source_document_id,
            draft_pages,
            requested_pages=range(1, total + 1),
            source_envelope={
                "source_sha256": identity.source_sha256,
                "page_count": total,
                "run_id": identity.run_id,
                "adapter_fingerprint": identity.adapter_fingerprint,
                "processing_fingerprint": identity.processing_fingerprint,
            },
        )
        all_attempts = [attempt for record in ordered_records for attempt in record.attempts]
        unknown_cost = sum((
            (attempt.reserved_cost_rub or Decimal(0))
            for attempt in all_attempts
            if attempt.request_started and attempt.actual_cost_rub is None
        ), Decimal(0))
        actual_known = sum((
            (attempt.actual_cost_rub or Decimal(0))
            for attempt in all_attempts if attempt.actual_cost_rub is not None
        ), Decimal(0))
        has_unknown_cost = any(
            attempt.request_started and attempt.actual_cost_rub is None for attempt in all_attempts
        )
        request_attempts = [attempt for attempt in all_attempts if attempt.request_started]
        actual_total: Decimal | None = None if not request_attempts or has_unknown_cost else actual_known

        def usage_total(field_name: str) -> int | None:
            if not request_attempts:
                return None
            values: list[int] = []
            for attempt in request_attempts:
                if attempt.usage is None or getattr(attempt.usage, field_name) is None:
                    return None
                values.append(getattr(attempt.usage, field_name))
            return sum(values)
        if cancel_event.is_set():
            status: Literal["completed", "completed_with_errors", "cancelled", "budget_exhausted"] = "cancelled"
        elif stop_scheduling.is_set():
            status = "budget_exhausted"
        elif any(record.state in ("failed", "unknown_outcome", "pending") for record in ordered_records):
            status = "completed_with_errors"
        else:
            status = "completed"
        return OrchestrationReport(
            status=status,
            run_id=identity.run_id,
            adapter=self.adapter.descriptor,
            config=self.config,
            source_document_id=identity.source_document_id,
            source_sha256=identity.source_sha256,
            expected_page_count=total,
            pages=ordered_records,
            draft=draft,
            elapsed_s=time.perf_counter() - started,
            max_parallel_requests=self._max_active_requests,
            max_inflight_image_count=self._max_images,
            max_inflight_image_bytes=self._max_image_bytes,
            actual_cost_rub=actual_total,
            reserved_unknown_cost_rub=unknown_cost,
            actual_input_tokens=usage_total("input_tokens"),
            actual_output_tokens=usage_total("output_tokens"),
            actual_reasoning_tokens=usage_total("reasoning_tokens"),
        )
