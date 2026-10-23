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
import math
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Awaitable, Callable, Literal, Protocol
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

from averon_import.services.ai_first.private_acl import (
    PrivateAclError, secure_private_tree, verify_private_path, verify_private_tree,
)

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


_SENSITIVE_DESCRIPTOR_KEY_PARTS = (
    "apikey", "apitoken", "secret", "credential", "authorization", "password",
    "accesstoken", "refreshtoken", "idtoken", "bearertoken", "privatekey",
)
_SENSITIVE_DESCRIPTOR_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?(?:key|token)|(?:access|refresh|id|bearer)[_-]?token|"
    r"secret|credential|authorization|password)\b\s*[:=]\s*\S+"
)
_BEARER_CREDENTIAL_VALUE = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_COMMON_OPAQUE_CREDENTIAL = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9_-]{20,}|ya29\.[A-Za-z0-9._-]{20,}|t1\.[A-Za-z0-9._-]{20,})"
)
_OPAQUE_DESCRIPTOR_TOKEN = re.compile(r"[A-Za-z0-9_.~+/-]{32,}={0,2}")


def _sensitive_descriptor_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    return any(part in normalized for part in _SENSITIVE_DESCRIPTOR_KEY_PARTS)


def _validate_public_descriptor_value(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str) or _sensitive_descriptor_key(key):
                raise ValueError("secret-like adapter parameter name")
            _validate_public_descriptor_value(nested)
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            _validate_public_descriptor_value(nested)
        return
    if not isinstance(value, str):
        return

    if (
        _SENSITIVE_DESCRIPTOR_ASSIGNMENT.search(value)
        or _BEARER_CREDENTIAL_VALUE.search(value)
        or _COMMON_OPAQUE_CREDENTIAL.search(value)
        or _OPAQUE_DESCRIPTOR_TOKEN.fullmatch(value)
    ):
        raise ValueError("credential-like adapter parameter value")
    for url in re.findall(r"(?i)https?://[^\s\"'<>]+", value):
        try:
            parsed = urlsplit(url.rstrip(".,;)]}"))
            query_keys = (key for key, _ in parse_qsl(parsed.query, keep_blank_values=True))
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("credential-like adapter parameter URL")
            if any(_sensitive_descriptor_key(key) for key in query_keys):
                raise ValueError("credential-like adapter parameter URL")
        except ValueError as exc:
            if str(exc).startswith("credential-like"):
                raise
            raise ValueError("invalid adapter parameter URL") from None


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
        for name in names:
            _validate_public_descriptor_value(name)
        _validate_public_descriptor_value(self.parameters)
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
    profile_id: str | None = Field(default=None, min_length=1, max_length=96)
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
    max_requests: int = Field(default=1000, ge=1, le=10000)
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
        pricing = self.pricing.model_dump(mode="json")
        if self.pricing.profile_id is None:
            # Keep pre-profile checkpoints compatible with the original pricing DTO.
            pricing.pop("profile_id", None)
        return _fingerprint({
            "dpi": self.dpi,
            "max_image_bytes": self.max_image_bytes,
            "max_image_pixels": self.max_image_pixels,
            "pricing": pricing,
        })


class CheckpointIdentity(_StrictModel):
    source_sha256: str
    source_size_bytes: int = Field(ge=1)
    source_mtime_ns: int = Field(ge=0)
    page_count: int = Field(ge=1)
    run_id: str
    source_document_id: str
    adapter_fingerprint: str
    private_adapter_scope_fingerprint: str | None = None
    processing_fingerprint: str

    @model_validator(mode="after")
    def valid_identity(self) -> CheckpointIdentity:
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_sha256):
            raise ValueError("invalid source digest")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,96}", self.run_id):
            raise ValueError("invalid run id")
        if self.source_document_id != f"pdf-sha256:{self.source_sha256}":
            raise ValueError("source id mismatch")
        if self.private_adapter_scope_fingerprint is not None and not re.fullmatch(
            r"[0-9a-f]{64}", self.private_adapter_scope_fingerprint,
        ):
            raise ValueError("invalid private adapter scope fingerprint")
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
    __slots__ = ("source_document_id", "page_number", "image", "run_id", "cancel_event")

    def __init__(
        self, source_document_id: str, page_number: int, image: PageImage, run_id: str,
        cancel_event: asyncio.Event | None = None,
    ):
        self.source_document_id = source_document_id
        self.page_number = page_number
        self.image = image
        self.run_id = run_id
        self.cancel_event = cancel_event


class AdapterPageResult(_StrictModel):
    extraction: AIPageExtraction
    usage: TokenUsage | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    finish_reason: Literal["stop", "length", "content_filter", "tool_calls", "unknown"] | None = None
    request_latency_ms: float | None = Field(default=None, ge=0)


class PageExtractionAdapter(Protocol):
    @property
    def descriptor(self) -> AdapterDescriptor: ...

    @property
    def private_checkpoint_scope_fingerprint(self) -> str | None: ...

    def estimate(self, image: PageImage) -> RequestEstimate: ...

    async def extract(self, request: PageExtractionRequest) -> AdapterPageResult: ...


class AdapterRequestError(RuntimeError):
    """Adapter failure with an explicit safe code and dispatch ambiguity."""

    def __init__(
        self, safe_code: str, *, outcome_unknown: bool, usage: TokenUsage | None = None,
        request_started: bool | None = None, http_status: int | None = None,
        finish_reason: str | None = None, request_latency_ms: float | None = None,
    ):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", safe_code):
            safe_code = "adapter_error"
        super().__init__(safe_code)
        self.safe_code = safe_code
        self.outcome_unknown = outcome_unknown
        self.usage = usage
        self.request_started = request_started
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        self.finish_reason = (
            finish_reason
            if finish_reason in ("stop", "length", "content_filter", "tool_calls", "unknown")
            else None
        )
        self.request_latency_ms = (
            float(request_latency_ms)
            if isinstance(request_latency_ms, (int, float))
            and not isinstance(request_latency_ms, bool)
            and math.isfinite(request_latency_ms)
            and request_latency_ms >= 0
            else None
        )


class PageAttempt(_StrictModel):
    state: Literal["running", "completed", "incomplete", "failed", "unknown_outcome"]
    started_at: str
    dispatched_at: str | None = None
    finished_at: str | None = None
    request_started: bool = False
    reserved_input_tokens: int | None = Field(default=None, ge=0)
    reserved_output_tokens: int | None = Field(default=None, ge=0)
    reserved_cost_rub: Decimal | None = Field(default=None, ge=0)
    actual_cost_rub: Decimal | None = Field(default=None, ge=0)
    usage: TokenUsage | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    finish_reason: Literal["stop", "length", "content_filter", "tool_calls", "unknown"] | None = None
    request_latency_ms: float | None = Field(default=None, ge=0)
    safe_error_code: str | None = None
    elapsed_s: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def attempt_consistency(self) -> PageAttempt:
        if self.state == "running" and self.finished_at is not None:
            raise ValueError("running attempt cannot have finish time")
        if self.state != "running" and self.finished_at is None:
            raise ValueError("finished attempt requires finish time")
        if self.state in ("incomplete", "failed", "unknown_outcome") and not self.safe_error_code:
            raise ValueError("failed attempt requires safe error code")
        if self.dispatched_at is not None and not self.request_started:
            raise ValueError("dispatched attempt must be marked started")
        if self.dispatched_at is not None:
            try:
                datetime.fromisoformat(self.dispatched_at)
            except ValueError:
                raise ValueError("invalid dispatch timestamp") from None
        return self


class PageRunRecord(_StrictModel):
    page_number: int = Field(ge=1)
    state: Literal["pending", "running", "completed", "incomplete", "failed", "unknown_outcome"]
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
        if self.state == "incomplete" and (
            self.extraction is None or self.extraction.status != "incomplete"
        ):
            raise ValueError("incomplete page requires an incomplete extraction")
        if self.extraction is not None and self.extraction.page_number != self.page_number:
            raise ValueError("record page mismatch")
        if self.state == "running" and (not self.attempts or self.attempts[-1].state != "running"):
            raise ValueError("running record requires running attempt")
        return self


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    try:
        before = path.stat()
        source_sha256 = _sha256_file(path)
        with fitz.open(path) as document:
            count = document.page_count
        after = path.stat()
    except Exception:
        raise OrchestrationError("source_pdf_invalid") from None
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OrchestrationError("source_changed_during_inspection")
    if count < 1 or count > config.max_pages:
        raise OrchestrationError("source_page_count_out_of_range")
    return CheckpointIdentity(
        source_sha256=source_sha256,
        source_size_bytes=after.st_size,
        source_mtime_ns=after.st_mtime_ns,
        page_count=count,
        run_id=run_id or uuid4().hex,
        source_document_id=f"pdf-sha256:{source_sha256}",
        adapter_fingerprint=adapter.descriptor.fingerprint,
        private_adapter_scope_fingerprint=getattr(
            adapter, "private_checkpoint_scope_fingerprint", None,
        ),
        processing_fingerprint=config.processing_fingerprint(),
    )


def _assert_source_signature(pdf_path: str | os.PathLike[str], identity: CheckpointIdentity) -> None:
    try:
        stat = Path(pdf_path).stat()
    except OSError:
        raise OrchestrationError("source_changed_during_run") from None
    if (stat.st_size, stat.st_mtime_ns) != (identity.source_size_bytes, identity.source_mtime_ns):
        raise OrchestrationError("source_changed_during_run")


def _verify_source_content(
    pdf_path: str | os.PathLike[str], identity: CheckpointIdentity,
) -> tuple[bool, str | None]:
    path = Path(pdf_path)
    try:
        before = path.stat()
        digest = _sha256_file(path)
        after = path.stat()
    except OSError:
        return False, "source_integrity_unavailable"
    expected_signature = (identity.source_size_bytes, identity.source_mtime_ns)
    before_signature = (before.st_size, before.st_mtime_ns)
    after_signature = (after.st_size, after.st_mtime_ns)
    if before_signature != after_signature or after_signature != expected_signature:
        return False, "source_changed_during_run"
    if digest != identity.source_sha256:
        return False, "source_integrity_mismatch"
    return True, None


class FitZPageRenderer:
    """Renders only the requested page and releases its PDF handle immediately."""

    def render(self, pdf_path: str | os.PathLike[str], page_number: int, dpi: int, config: OrchestrationConfig) -> PageImage:
        try:
            with fitz.open(pdf_path) as document:
                if page_number < 1 or page_number > document.page_count:
                    raise OrchestrationError("source_page_out_of_range")
                page = document.load_page(page_number - 1)
                scale = dpi / 72
                rect = page.rect
                projected_width = float(rect.width) * scale
                projected_height = float(rect.height) * scale
                if (
                    not math.isfinite(projected_width)
                    or not math.isfinite(projected_height)
                    or projected_width <= 0
                    or projected_height <= 0
                ):
                    raise OrchestrationError("page_dimensions_invalid")
                if math.ceil(projected_width) * math.ceil(projected_height) > config.max_image_pixels:
                    raise OrchestrationError("image_pixel_limit")
                pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
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
            "format_version": 2,
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
            verify_private_tree(self.directory)
            return records
        except PrivateAclError:
            self.close()
            raise OrchestrationError("checkpoint_permissions_failed") from None
        except OrchestrationError:
            self.close()
            raise
        except Exception:
            self.close()
            raise OrchestrationError("checkpoint_invalid") from None

    def _secure_tree(self) -> None:
        try:
            secure_private_tree(self.directory)
        except PrivateAclError:
            raise OrchestrationError("checkpoint_permissions_failed") from None

    def _acquire_process_lock(self) -> None:
        lock_path = self.directory / ".run.lock"
        self._lock_stream = lock_path.open("a+b")
        try:
            if os.name != "nt":
                os.chmod(lock_path, 0o600)
            verify_private_path(lock_path, directory=False)
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
        except PrivateAclError:
            self.close()
            raise OrchestrationError("checkpoint_permissions_failed") from None
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
                verify_private_path(temp_name, directory=False)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
            temp_name = None
            verify_private_path(path, directory=False)
            if os.name != "nt":
                descriptor = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except PrivateAclError:
            raise OrchestrationError("checkpoint_permissions_failed") from None
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
    RESTORE_SAFETY_MARGIN_S = 1.0

    def __init__(
        self, rpm: int, tpm: int, *, window_s: float = 60.0,
        records: dict[int, PageRunRecord] | None = None,
    ):
        self.rpm = rpm
        self.tpm = tpm
        self.window_s = window_s
        self.events: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()
        self._restore_events(records or {})

    def _restore_events(self, records: dict[int, PageRunRecord]) -> None:
        if not records:
            return
        loop = asyncio.get_running_loop()
        now_monotonic = loop.time()
        now_utc = datetime.now(timezone.utc)
        restored: list[tuple[float, int]] = []
        for record in records.values():
            for attempt in record.attempts:
                if not attempt.request_started:
                    continue
                timestamp = attempt.dispatched_at
                if timestamp is None:
                    # Legacy or incomplete dispatch evidence consumes a full cooldown.
                    age_s = 0.0
                else:
                    try:
                        dispatched = datetime.fromisoformat(timestamp)
                        if dispatched.tzinfo is None:
                            dispatched = dispatched.replace(tzinfo=timezone.utc)
                        age_s = max(0.0, (now_utc - dispatched.astimezone(timezone.utc)).total_seconds())
                    except (TypeError, ValueError):
                        age_s = 0.0
                    age_s = max(0.0, age_s - self.RESTORE_SAFETY_MARGIN_S)
                if age_s >= self.window_s:
                    continue
                if attempt.reserved_input_tokens is None or attempt.reserved_output_tokens is None:
                    token_reservation = self.tpm
                else:
                    token_reservation = attempt.reserved_input_tokens + attempt.reserved_output_tokens
                restored.append((now_monotonic - age_s, token_reservation))
        self.events.extend(sorted(restored))

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
    state: Literal["pending", "running", "completed", "incomplete", "failed", "unknown_outcome"]
    finished_pages: int = Field(ge=0)
    total_pages: int = Field(ge=1)


class OrchestrationReport(_StrictModel):
    status: Literal["completed", "completed_with_errors", "cancelled", "budget_exhausted"]
    run_id: str
    adapter: AdapterDescriptor
    config: OrchestrationConfig
    source_document_id: str
    source_sha256: str
    source_integrity_verified: bool
    source_integrity_error_code: str | None = None
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
        if self.status == "completed" and not self.source_integrity_verified:
            raise ValueError("completed report requires verified source integrity")
        if self.source_integrity_verified and self.source_integrity_error_code is not None:
            raise ValueError("verified source cannot have an integrity error")
        if not self.source_integrity_verified and (
            self.source_integrity_error_code is None
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.source_integrity_error_code)
        ):
            raise ValueError("unverified source requires a safe integrity error")
        if not self.source_integrity_verified and any(page.status != "failed" for page in self.draft.pages):
            raise ValueError("unverified source draft must fail every page")
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
        selected_pages: set[int] | None = None,
    ) -> OrchestrationReport:
        try:
            return await self._run_impl(
                pdf_path, cancel_event=cancel_event, retry_pages=retry_pages,
                selected_pages=selected_pages,
            )
        finally:
            self.checkpoint.close()

    async def _run_impl(
        self,
        pdf_path: str | os.PathLike[str],
        *,
        cancel_event: asyncio.Event | None = None,
        retry_pages: set[int] | None = None,
        selected_pages: set[int] | None = None,
    ) -> OrchestrationReport:
        started = time.perf_counter()
        cancel_event = cancel_event or asyncio.Event()
        identity = self.checkpoint.identity
        page_selection = set(range(1, identity.page_count + 1)) if selected_pages is None else set(selected_pages)
        if not page_selection or any(
            type(page) is not int or page < 1 or page > identity.page_count
            for page in page_selection
        ):
            raise OrchestrationError("invalid_page_selection")
        if identity.adapter_fingerprint != self.adapter.descriptor.fingerprint:
            raise OrchestrationError("adapter_fingerprint_mismatch")
        if identity.processing_fingerprint != self.config.processing_fingerprint():
            raise OrchestrationError("processing_fingerprint_mismatch")
        adapter_rate_limits = getattr(self.adapter, "rate_limits", None)
        if adapter_rate_limits is not None and adapter_rate_limits != (
            self.config.requests_per_minute, self.config.tokens_per_minute,
        ):
            raise OrchestrationError("adapter_rate_limit_mismatch")
        adapter_pricing_profile = getattr(self.adapter, "pricing_profile_id", None)
        if adapter_pricing_profile is not None and adapter_pricing_profile != self.config.pricing.profile_id:
            raise OrchestrationError("adapter_pricing_profile_mismatch")
        adapter_request_limit = getattr(self.adapter, "max_requests", None)
        if adapter_request_limit is not None and (
            type(adapter_request_limit) is not int or adapter_request_limit < 1
        ):
            raise OrchestrationError("adapter_request_limit_invalid")
        adapter_page_limit = getattr(self.adapter, "max_pages", None)
        if adapter_page_limit is not None:
            if type(adapter_page_limit) is not int or adapter_page_limit < 1:
                raise OrchestrationError("adapter_page_limit_invalid")
            if len(page_selection) > adapter_page_limit:
                raise OrchestrationError("adapter_page_limit_exceeded")
        actual_identity = inspect_pdf(pdf_path, self.adapter, self.config, run_id=identity.run_id)
        if actual_identity != identity:
            raise OrchestrationError("source_or_configuration_identity_mismatch")
        records = self.checkpoint.open()
        total = identity.page_count
        retry_pages = set(retry_pages or ())
        if any(type(page) is not int or page < 1 or page > total for page in retry_pages):
            self.checkpoint.close()
            raise OrchestrationError("invalid_retry_page")
        if not retry_pages.issubset(page_selection):
            self.checkpoint.close()
            raise OrchestrationError("retry_page_not_selected")
        for page_number in retry_pages:
            current = records.get(page_number)
            if current is None or current.state not in ("failed", "incomplete", "unknown_outcome"):
                self.checkpoint.close()
                raise OrchestrationError("retry_requires_retryable_page")
            reset = current.model_copy(update={"state": "pending", "updated_at": _utc_now()})
            records[page_number] = reset
            await self.checkpoint.write(reset)

        pending = []
        for number in range(1, total + 1):
            record = records.get(number)
            if record is None:
                record = PageRunRecord(page_number=number, state="pending", updated_at=_utc_now())
                records[number] = record
            if record.state == "pending" and number in page_selection:
                pending.append(number)

        rate_gate = _RateGate(
            self.config.requests_per_minute, self.config.tokens_per_minute,
            window_s=self.rate_window_s, records=records,
        )
        budget = _BudgetLedger(self.config.max_budget_rub, records)
        adapter_request_limit = getattr(self.adapter, "max_requests", None)
        request_limit = min(self.config.max_requests, adapter_request_limit or self.config.max_requests)
        requests_reserved = sum(
            1 for record in records.values() for attempt in record.attempts
            if attempt.request_started
        )
        request_slots_lock = asyncio.Lock()
        queue: asyncio.Queue[int | None] = asyncio.Queue(maxsize=max(1, self.config.concurrency * 2))
        stop_scheduling = asyncio.Event()
        stop_code: str | None = None
        finished = sum(
            1 for record in records.values()
            if record.state in ("completed", "incomplete", "failed", "unknown_outcome")
        )

        async def notify(page_number: int, state: str) -> None:
            nonlocal finished
            if state in ("completed", "incomplete", "failed", "unknown_outcome"):
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

        async def reserve_request_slot() -> bool:
            nonlocal requests_reserved
            async with request_slots_lock:
                if requests_reserved >= request_limit:
                    return False
                requests_reserved += 1
                return True

        async def release_request_slot() -> None:
            nonlocal requests_reserved
            async with request_slots_lock:
                requests_reserved = max(0, requests_reserved - 1)

        async def produce() -> None:
            for number in pending:
                if cancel_event.is_set() or stop_scheduling.is_set():
                    break
                await queue.put(number)
            for _ in range(self.config.concurrency):
                await queue.put(None)

        async def process_page(number: int) -> None:
            nonlocal stop_code
            current = records[number]
            try:
                _assert_source_signature(pdf_path, identity)
                image = await asyncio.to_thread(
                    self.renderer.render, pdf_path, number, self.config.dpi, self.config,
                )
                _assert_source_signature(pdf_path, identity)
            except OrchestrationError as exc:
                if exc.code == "source_changed_during_run":
                    stop_code = exc.code
                    stop_scheduling.set()
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
                try:
                    _assert_source_signature(pdf_path, identity)
                except OrchestrationError as source_exc:
                    if source_exc.code == "source_changed_during_run":
                        stop_code = source_exc.code
                        stop_scheduling.set()
                        safe_error_code = source_exc.code
                    else:
                        safe_error_code = "page_render_failed"
                else:
                    safe_error_code = "page_render_failed"
                records[number] = current.model_copy(update={
                    "state": "failed", "updated_at": _utc_now(),
                    "attempts": [*current.attempts, PageAttempt(
                        state="failed", started_at=_utc_now(), finished_at=_utc_now(),
                        safe_error_code=safe_error_code, elapsed_s=0.0,
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
                stop_code = "budget_exhausted"
                stop_scheduling.set()
                return

            attempt = PageAttempt(
                state="running", started_at=_utc_now(),
                request_started=False,
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
            async def stop_before_dispatch(code: str, *, terminal: bool = False) -> None:
                latest = running.attempts[-1].model_copy(update={
                    "state": "failed", "request_started": False,
                    "finished_at": _utc_now(), "safe_error_code": code,
                    "elapsed_s": time.perf_counter() - request_started_at,
                })
                await budget.settle(reserve, None, request_started=False)
                records[number] = running.model_copy(update={
                    "state": "failed" if terminal else "pending",
                    "attempts": [*running.attempts[:-1], latest],
                    "updated_at": _utc_now(),
                })
                self._active_images.pop(number, None)
                await self.checkpoint.write(records[number])
                await notify(number, "failed" if terminal else "pending")

            if cancel_event.is_set():
                await stop_before_dispatch("cancelled_before_dispatch")
                return
            try:
                _assert_source_signature(pdf_path, identity)
            except OrchestrationError as exc:
                stop_code = exc.code
                stop_scheduling.set()
                await stop_before_dispatch(exc.code, terminal=True)
                return
            if not await rate_gate.acquire(estimate.reserved_input_tokens + estimate.reserved_output_tokens, cancel_event, stop_scheduling):
                code = "cancelled_before_dispatch" if cancel_event.is_set() else (
                    "budget_stopped_before_dispatch" if stop_code == "budget_exhausted"
                    else stop_code or "orchestration_stopped_before_dispatch"
                )
                await stop_before_dispatch(
                    code, terminal=stop_code not in (None, "budget_exhausted") and not cancel_event.is_set(),
                )
                return
            if cancel_event.is_set():
                await stop_before_dispatch("cancelled_before_dispatch")
                return
            try:
                _assert_source_signature(pdf_path, identity)
            except OrchestrationError as exc:
                stop_code = exc.code
                stop_scheduling.set()
                await stop_before_dispatch(exc.code, terminal=True)
                return
            if stop_scheduling.is_set():
                code = stop_code or "orchestration_stopped_before_dispatch"
                await stop_before_dispatch(
                    code, terminal=stop_code not in (None, "budget_exhausted"),
                )
                return

            if not await reserve_request_slot():
                stop_code = "request_limit_exhausted"
                stop_scheduling.set()
                await stop_before_dispatch(stop_code, terminal=True)
                return

            dispatched_at = _utc_now()
            dispatched_attempt = running.attempts[-1].model_copy(update={
                "request_started": True,
                "dispatched_at": dispatched_at,
            })
            running = running.model_copy(update={
                "attempts": [*running.attempts[:-1], dispatched_attempt],
                "updated_at": dispatched_at,
            })
            records[number] = running
            await self.checkpoint.write(running)

            response: AdapterPageResult | None = None
            error_state: str | None = None
            error_code: str | None = None
            error_usage: TokenUsage | None = None
            error_request_started: bool | None = None
            error_http_status: int | None = None
            error_finish_reason: str | None = None
            error_request_latency_ms: float | None = None
            request_task = asyncio.create_task(self.adapter.extract(PageExtractionRequest(
                identity.source_document_id, number, image, identity.run_id,
                cancel_event=cancel_event,
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
                    request_result = await asyncio.gather(request_task, return_exceptions=True)
                    settled = request_result[0]
                    if isinstance(settled, AdapterPageResult):
                        response = settled
                    elif isinstance(settled, AdapterRequestError):
                        error_state = "unknown_outcome" if settled.outcome_unknown else "failed"
                        error_code = settled.safe_code
                        error_usage = settled.usage
                        error_request_started = settled.request_started
                        error_http_status = settled.http_status
                        error_finish_reason = settled.finish_reason
                        error_request_latency_ms = settled.request_latency_ms
                    else:
                        error_state, error_code = "unknown_outcome", "cancelled_after_dispatch"
            except AdapterRequestError as exc:
                error_state = "unknown_outcome" if exc.outcome_unknown else "failed"
                error_code = exc.safe_code
                error_usage = exc.usage
                error_request_started = exc.request_started
                error_http_status = exc.http_status
                error_finish_reason = exc.finish_reason
                error_request_latency_ms = exc.request_latency_ms
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
            http_status = error_http_status
            finish_reason = error_finish_reason
            request_latency_ms = error_request_latency_ms
            if response is not None and not isinstance(response, AdapterPageResult):
                error_state, error_code = "failed", "adapter_response_invalid"
                response = None
            if response is not None:
                usage = response.usage
                actual_cost = self.config.pricing.actual_cost(usage) if usage is not None else None
                http_status = response.http_status
                finish_reason = response.finish_reason
                request_latency_ms = response.request_latency_ms
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
                elif extraction.status == "incomplete":
                    error_state, error_code = "incomplete", "transcription_incomplete"

            if error_request_started is False:
                await release_request_slot()

            old_attempts = running.attempts
            final_state = error_state or "completed"
            attempt_request_started = True if error_request_started is None else error_request_started
            updated_attempt = old_attempts[-1].model_copy(update={
                "state": final_state,
                "request_started": attempt_request_started,
                "dispatched_at": old_attempts[-1].dispatched_at if attempt_request_started else None,
                "finished_at": _utc_now(),
                "actual_cost_rub": actual_cost,
                "usage": usage,
                "http_status": http_status,
                "finish_reason": finish_reason,
                "request_latency_ms": request_latency_ms,
                "safe_error_code": error_code,
                "elapsed_s": time.perf_counter() - request_started_at,
            })
            if await budget.settle(reserve, actual_cost, request_started=attempt_request_started):
                if stop_code is None:
                    stop_code = "budget_exhausted"
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

        source_integrity_verified, source_integrity_error_code = _verify_source_content(pdf_path, identity)
        if stop_code == "source_changed_during_run":
            source_integrity_verified = False
            source_integrity_error_code = stop_code
        if not source_integrity_verified:
            stop_code = source_integrity_error_code or "source_integrity_unavailable"
            for number, record in list(records.items()):
                if record.state != "completed":
                    continue
                attempts = list(record.attempts)
                now = _utc_now()
                if attempts:
                    attempts[-1] = attempts[-1].model_copy(update={
                        "state": "failed",
                        "safe_error_code": stop_code,
                    })
                else:
                    attempts.append(PageAttempt(
                        state="failed", started_at=now, finished_at=now,
                        safe_error_code=stop_code,
                    ))
                records[number] = record.model_copy(update={
                    "state": "failed", "attempts": attempts, "updated_at": now,
                })
                await self.checkpoint.write(records[number])

        for number in range(1, total + 1):
            if number not in records:
                records[number] = PageRunRecord(page_number=number, state="pending", updated_at=_utc_now())
        ordered_records = [records[n] for n in range(1, total + 1)]
        draft_pages: list[AIPageExtraction] = []
        if not source_integrity_verified:
            draft_pages = [
                failed_page(identity.source_document_id, number, source_integrity_error_code or "source_integrity_unavailable")
                for number in range(1, total + 1)
            ]
        else:
            for record in ordered_records:
                extraction = record.extraction
                if (
                    record.state == "incomplete"
                    and extraction is not None
                    and extraction.status == "incomplete"
                ):
                    # Preserve the partial transcription for explicit human review/retry.
                    # The V2A export guard rejects pages with incomplete status.
                    draft_pages.append(extraction)
                    continue
                if (
                    record.state == "completed"
                    and extraction is not None
                    and extraction.status in {"extracted", "no_specification"}
                ):
                    draft_pages.append(record.extraction)
                    continue

                if record.state == "completed":
                    code = (
                        "page_extraction_incomplete"
                        if extraction is not None and extraction.status == "incomplete"
                        else "page_extraction_failed"
                    )
                else:
                    code = record.attempts[-1].safe_error_code if record.attempts else None
                    if code is not None and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
                        code = None
                    if code is None:
                        if stop_scheduling.is_set():
                            code = (
                                "budget_exhausted_before_dispatch"
                                if stop_code == "budget_exhausted"
                                else stop_code or "orchestration_stopped_before_dispatch"
                            )
                        elif cancel_event.is_set():
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
        status: Literal["completed", "completed_with_errors", "cancelled", "budget_exhausted"]
        if not source_integrity_verified:
            status = "completed_with_errors"
        elif cancel_event.is_set():
            status = "cancelled"
        elif stop_code == "budget_exhausted":
            status = "budget_exhausted"
        elif stop_code is not None:
            status = "completed_with_errors"
        elif stop_scheduling.is_set():
            status = "budget_exhausted"
        elif any(
            record.state in ("incomplete", "failed", "unknown_outcome", "pending", "running")
            or record.extraction is None
            or record.extraction.status not in {"extracted", "no_specification"}
            for record in ordered_records
        ):
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
            source_integrity_verified=source_integrity_verified,
            source_integrity_error_code=source_integrity_error_code,
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
