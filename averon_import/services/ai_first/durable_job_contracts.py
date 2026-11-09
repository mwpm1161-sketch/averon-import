"""B3.2 typed inputs and bounded public status DTOs (no API or scheduler)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import re
from pathlib import Path

from .coordination_contracts import (
    CoordinationError, Run, Binding, canonical, digest, fingerprint, identifier, integer, rub_units, uuid_hex,
)
from .orchestration import OrchestrationConfig

MAX_REQUEST_BYTES = 262_144
MAX_EXECUTION_BYTES = 262_144
MAX_PROMPT_BYTES = 65_536
MAX_SELECTED_PAGES = 1_000
MAX_PUBLIC_PAGE_SIZE = 100
MAX_SOURCE_BYTES = 1_073_741_824

_AUTH_SEAL = object()
_SECRET_TEXT = re.compile(
    r"(?i)(?:\bsk-[A-Za-z0-9_-]{20,}|\bya29\.[A-Za-z0-9._-]{20,}|"
    r"\b(?:api[_-]?(?:key|token)|authorization|bearer[_-]?token|password|private[_-]?key)"
    r"\s*[:=]\s*\S+)"
)


@dataclass(frozen=True)
class JobPageDispatchClaim:
    """Trusted internal scheduler-to-dispatcher fence for one durable page."""
    tenant_id: str
    owner_id: str
    document_id: str
    job_id: str
    run_id: str
    generation: int
    page_number: int
    candidate_attempt_id: str
    claim_seq: int
    page_revision: int
    owner_epoch: int
    cancel_revision: int

    def __post_init__(self):
        for value in (self.tenant_id, self.owner_id, self.document_id):
            identifier(value)
        if (type(self.job_id) is not str or not re.fullmatch(r"dj_[0-9a-f]{32}", self.job_id)):
            raise CoordinationError("invalid_job_dispatch_claim")
        for value in (self.run_id, self.candidate_attempt_id):
            uuid_hex(value)
        for value in (self.generation, self.page_number, self.claim_seq,
                      self.page_revision, self.owner_epoch):
            integer(value, minimum=1)
        integer(self.cancel_revision)


def _json_bytes(value, maximum):
    try:
        raw = canonical(value).encode("utf-8")
    except (TypeError, ValueError, RecursionError, OverflowError):
        raise CoordinationError("durable_input_invalid") from None
    if not raw or len(raw) > maximum:
        raise CoordinationError("durable_input_too_large")
    return raw


def _text_bytes(value, code):
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError:
        raise CoordinationError(code) from None


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_json_key")
        value[key] = item
    return value


def parse_json_unique(raw: bytes | str, *, maximum: int):
    if type(raw) is bytes:
        if len(raw) > maximum:
            raise CoordinationError("durable_input_too_large")
        try:
            raw = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise CoordinationError("durable_input_invalid") from None
    if type(raw) is not str or len(_text_bytes(raw, "durable_input_invalid")) > maximum:
        raise CoordinationError("durable_input_too_large")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_pairs,
                           parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, RecursionError, json.JSONDecodeError):
        raise CoordinationError("durable_input_invalid") from None
    return value


@dataclass(frozen=True, repr=False)
class AuthorizedDocumentContext:
    """Server-issued proof that this actor may use this current document.

    The source path is an internal resolver result and is deliberately hidden
    from repr, JSON, receipts and status. HTTP adapters do not construct this
    type from caller-supplied tenant/owner/path values.
    """
    tenant_id: str
    owner_id: str
    document_id: str
    generation: int
    source_digest: str
    source_size_bytes: int
    source_mtime_ns: int
    page_count: int
    source_path: Path = field(repr=False)
    authorization_digest: str = field(repr=False)
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self):
        if self._seal is not _AUTH_SEAL:
            raise CoordinationError("server_authorization_context_required")
        for value in (self.tenant_id, self.owner_id, self.document_id):
            identifier(value)
        integer(self.generation, minimum=1)
        digest(self.source_digest)
        integer(self.source_size_bytes, minimum=1)
        integer(self.source_mtime_ns)
        integer(self.page_count, minimum=1)
        digest(self.authorization_digest)
        if not isinstance(self.source_path, Path) or not self.source_path.is_absolute():
            raise CoordinationError("document_authorization_invalid")


def _verified_document_context(*, tenant_id, owner_id, document_id, generation,
                               source_digest, source_size_bytes, source_mtime_ns,
                               page_count, source_path, authorization_digest):
    """Internal adapter seam for an already-authenticated server component."""
    return AuthorizedDocumentContext(tenant_id, owner_id, document_id, generation, source_digest,
        source_size_bytes, source_mtime_ns, page_count, Path(source_path), authorization_digest, _AUTH_SEAL)


@dataclass(frozen=True)
class PageSelection:
    page_number: int
    crop: tuple[float, float, float, float] | None = None

    def __post_init__(self):
        integer(self.page_number, minimum=1)
        if self.crop is not None:
            if type(self.crop) is not tuple or len(self.crop) != 4:
                raise CoordinationError("invalid_page_selection")
            x, y, width, height = self.crop
            if (any(type(value) not in (int, float) or isinstance(value, bool)
                    or not math.isfinite(value) for value in self.crop)
                    or x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1):
                raise CoordinationError("invalid_page_selection")


@dataclass(frozen=True)
class SubmissionRequest:
    """Exact caller command body; financial/current settings are not part of it."""
    operation_id: str
    document_id: str
    pages: tuple[PageSelection, ...]
    user_context: str = field(default="", repr=False)

    def __post_init__(self):
        uuid_hex(self.operation_id)
        identifier(self.document_id)
        if type(self.pages) is not tuple or not self.pages or len(self.pages) > MAX_SELECTED_PAGES:
            raise CoordinationError("invalid_page_selection")
        if any(type(page) is not PageSelection for page in self.pages):
            raise CoordinationError("invalid_page_selection")
        numbers = tuple(page.page_number for page in self.pages)
        if tuple(sorted(set(numbers))) != numbers:
            raise CoordinationError("invalid_page_selection")
        if type(self.user_context) is not str or len(_text_bytes(self.user_context, "durable_input_invalid")) > MAX_PROMPT_BYTES:
            raise CoordinationError("durable_input_too_large")
        if _SECRET_TEXT.search(self.user_context):
            raise CoordinationError("execution_secret_rejected")
        _json_bytes(asdict(self), MAX_REQUEST_BYTES)

    @property
    def request_digest(self):
        return fingerprint({"document_id": self.document_id,
                            "pages": [asdict(page) for page in self.pages],
                            "user_context": self.user_context})


def parse_submission_request(raw: bytes | str) -> SubmissionRequest:
    value = parse_json_unique(raw, maximum=MAX_REQUEST_BYTES)
    if type(value) is not dict or set(value) != {"operation_id", "document_id", "pages", "user_context"}:
        raise CoordinationError("durable_input_invalid")
    if type(value["pages"]) is not list:
        raise CoordinationError("durable_input_invalid")
    pages = []
    for item in value["pages"]:
        if type(item) is not dict or set(item) != {"page_number", "crop"}:
            raise CoordinationError("durable_input_invalid")
        crop = item["crop"]
        if crop is not None:
            if type(crop) is not list or len(crop) != 4:
                raise CoordinationError("durable_input_invalid")
            crop = tuple(crop)
        pages.append(PageSelection(item["page_number"], crop))
    if type(value["user_context"]) is not str:
        raise CoordinationError("durable_input_invalid")
    return SubmissionRequest(value["operation_id"], value["document_id"], tuple(pages), value["user_context"])


@dataclass(frozen=True)
class FinancialSnapshotInput:
    run: Run
    config: OrchestrationConfig
    binding: Binding

    def __post_init__(self):
        if type(self.run) is not Run or type(self.binding) is not Binding or type(self.config) is not OrchestrationConfig:
            raise CoordinationError("financial_snapshot_invalid")


@dataclass(frozen=True)
class ExecutionProfile:
    """Server-selected prompt/model names; never accepts endpoint/auth/body."""
    provider_id: str
    model_id: str
    model_version: str
    schema_version: str
    prompt_version: str
    prompt_template: str = field(repr=False)

    def __post_init__(self):
        for value in (self.provider_id, self.model_id, self.model_version,
                      self.schema_version, self.prompt_version):
            if (type(value) is not str or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,160}", value)
                    or "http" in value.casefold()):
                raise CoordinationError("execution_profile_invalid")
        if (type(self.prompt_template) is not str
                or not self.prompt_template.strip()
                or len(_text_bytes(self.prompt_template, "execution_prompt_invalid")) > MAX_PROMPT_BYTES
                or _SECRET_TEXT.search(self.prompt_template)):
            raise CoordinationError("execution_prompt_invalid")


@dataclass(frozen=True)
class ExecutionInput:
    """Canonical private execution preimage. It is not a wire body or OCR result."""
    version: int
    tenant_id: str
    document_id: str
    generation: int
    run_id: str
    source_sha256: str
    source_size_bytes: int
    source_mtime_ns: int
    page_count: int
    pages: tuple[PageSelection, ...]
    dpi: int
    max_image_bytes: int
    max_image_pixels: int
    provider_id: str
    model_id: str
    model_version: str
    schema_version: str
    prompt_version: str
    prompt_template: str = field(repr=False)
    user_context: str = field(repr=False)
    prompt_sha256: str

    def __post_init__(self):
        if type(self.version) is not int or self.version != 1:
            raise CoordinationError("execution_version_unsupported")
        for value in (self.tenant_id, self.document_id):
            identifier(value)
        uuid_hex(self.run_id)
        integer(self.generation, minimum=1)
        digest(self.source_sha256)
        integer(self.source_size_bytes, minimum=1)
        integer(self.source_mtime_ns)
        integer(self.page_count, minimum=1)
        integer(self.dpi, minimum=36)
        integer(self.max_image_bytes, minimum=1)
        integer(self.max_image_pixels, minimum=1)
        if (self.dpi > 600 or self.max_image_bytes > 100_000_000
                or self.max_image_pixels > 100_000_000):
            raise CoordinationError("execution_limits_invalid")
        if type(self.pages) is not tuple or not self.pages or len(self.pages) > MAX_SELECTED_PAGES:
            raise CoordinationError("invalid_page_selection")
        if any(page.page_number > self.page_count for page in self.pages):
            raise CoordinationError("invalid_page_selection")
        if tuple(sorted(set(page.page_number for page in self.pages))) != tuple(page.page_number for page in self.pages):
            raise CoordinationError("invalid_page_selection")
        profile = ExecutionProfile(self.provider_id, self.model_id, self.model_version,
            self.schema_version, self.prompt_version, self.prompt_template)
        if (type(self.user_context) is not str or len(_text_bytes(self.user_context, "execution_prompt_invalid")) > MAX_PROMPT_BYTES
                or _SECRET_TEXT.search(self.user_context)):
            raise CoordinationError("execution_prompt_invalid")
        if self.prompt_sha256 != hashlib.sha256(profile.prompt_template.encode("utf-8")).hexdigest():
            raise CoordinationError("execution_prompt_digest_mismatch")
        _json_bytes(self.to_dict(), MAX_EXECUTION_BYTES)

    def to_dict(self):
        return {
            "version": self.version,
            "document": {"tenant_id": self.tenant_id, "document_id": self.document_id,
                "generation": self.generation, "run_id": self.run_id,
                "source_sha256": self.source_sha256, "source_size_bytes": self.source_size_bytes,
                "source_mtime_ns": self.source_mtime_ns, "page_count": self.page_count},
            "pages": [asdict(page) for page in self.pages],
            "ocr": {"dpi": self.dpi, "max_image_bytes": self.max_image_bytes,
                    "max_image_pixels": self.max_image_pixels},
            "adapter": {"provider_id": self.provider_id, "model_id": self.model_id,
                "model_version": self.model_version, "schema_version": self.schema_version},
            "prompt": {"version": self.prompt_version, "text": self.prompt_template,
                       "user_context": self.user_context, "sha256": self.prompt_sha256},
        }

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != {"version", "document", "pages", "ocr", "adapter", "prompt"}:
            raise CoordinationError("execution_input_invalid")
        document, ocr, adapter, prompt = value["document"], value["ocr"], value["adapter"], value["prompt"]
        expected = (
            (document, {"tenant_id", "document_id", "generation", "run_id", "source_sha256",
                        "source_size_bytes", "source_mtime_ns", "page_count"}),
            (ocr, {"dpi", "max_image_bytes", "max_image_pixels"}),
            (adapter, {"provider_id", "model_id", "model_version", "schema_version"}),
            (prompt, {"version", "text", "user_context", "sha256"}),
        )
        if any(type(item) is not dict or set(item) != keys for item, keys in expected):
            raise CoordinationError("execution_input_invalid")
        if type(value["pages"]) is not list:
            raise CoordinationError("execution_input_invalid")
        pages = []
        for item in value["pages"]:
            if type(item) is not dict or set(item) != {"page_number", "crop"}:
                raise CoordinationError("execution_input_invalid")
            pages.append(PageSelection(item["page_number"], tuple(item["crop"]) if item["crop"] is not None else None))
        return cls(value["version"], document["tenant_id"], document["document_id"], document["generation"],
            document["run_id"], document["source_sha256"], document["source_size_bytes"],
            document["source_mtime_ns"], document["page_count"], tuple(pages),
            ocr["dpi"], ocr["max_image_bytes"], ocr["max_image_pixels"],
            adapter["provider_id"], adapter["model_id"], adapter["model_version"], adapter["schema_version"],
            prompt["version"], prompt["text"], prompt["user_context"], prompt["sha256"])


def build_execution_input(context: AuthorizedDocumentContext, request: SubmissionRequest,
                          snapshot: FinancialSnapshotInput, profile: ExecutionProfile) -> ExecutionInput:
    if (type(context) is not AuthorizedDocumentContext or type(request) is not SubmissionRequest
            or type(snapshot) is not FinancialSnapshotInput or type(profile) is not ExecutionProfile):
        raise CoordinationError("trusted_submission_context_required")
    run, config = snapshot.run, snapshot.config
    if ((context.tenant_id, context.document_id, context.generation, context.source_digest)
            != (run.tenant_id, run.document_id, run.generation, run.source_digest)
            or request.document_id != context.document_id
            or context.page_count > config.max_pages
            or any(page.page_number > context.page_count for page in request.pages)):
        raise CoordinationError("document_identity_mismatch")
    prompt_digest = hashlib.sha256(profile.prompt_template.encode("utf-8")).hexdigest()
    return ExecutionInput(1, context.tenant_id, context.document_id, context.generation, run.run_id,
        context.source_digest, context.source_size_bytes, context.source_mtime_ns, context.page_count,
        request.pages, config.dpi, config.max_image_bytes, config.max_image_pixels,
        profile.provider_id, profile.model_id, profile.model_version, profile.schema_version,
        profile.prompt_version, profile.prompt_template, request.user_context, prompt_digest)


def execution_bytes(value: ExecutionInput):
    return _json_bytes(value.to_dict(), MAX_EXECUTION_BYTES)


def parse_execution_bytes(raw):
    value = parse_json_unique(raw, maximum=MAX_EXECUTION_BYTES)
    parsed = ExecutionInput.from_dict(value)
    if execution_bytes(parsed) != raw:
        raise CoordinationError("execution_canonical_mismatch")
    return parsed


@dataclass(frozen=True)
class QueueLimits:
    active_jobs_global: int = 256
    active_jobs_per_owner: int = 32
    active_pages_global: int = 20_000
    active_pages_per_owner: int = 2_000

    def __post_init__(self):
        for value in (self.active_jobs_global, self.active_jobs_per_owner,
                      self.active_pages_global, self.active_pages_per_owner):
            integer(value, minimum=1)
        if (self.active_jobs_global > 256 or self.active_jobs_per_owner > 32
                or self.active_pages_global > 20_000 or self.active_pages_per_owner > 2_000):
            raise CoordinationError("queue_limits_exceed_compiled_ceiling")


@dataclass(frozen=True)
class SubmissionReceipt:
    operation_id: str
    job_id: str
    run_id: str
    snapshot_id: str
    request_digest: str
    revision: int


@dataclass(frozen=True)
class CommandReceipt:
    operation_id: str
    job_id: str
    kind: str
    revision: int
    state: str
    request_digest: str


@dataclass(frozen=True)
class PageStatus:
    page_number: int
    state: str
    revision: int
    safe_code: str | None
    result_bytes: int


@dataclass(frozen=True)
class JobStatusSnapshot:
    job_id: str
    run_id: str
    document_id: str
    generation: int
    state: str
    revision: int
    cancel_revision: int
    created_at: str
    updated_at: str
    finished_at: str | None
    publication_state: str
    pages: tuple[PageStatus, ...]
    next_page_cursor: int | None


def receipt_json(value):
    if type(value) not in (SubmissionReceipt, CommandReceipt):
        raise CoordinationError("receipt_invalid")
    return canonical(asdict(value))


def public_safe_code(value):
    allowed = {"execution_input_missing", "execution_input_corrupt", "execution_source_changed",
               "execution_source_mismatch", "cancelled", "dispatch_unknown", "quota_deferred",
               "page_failed", "result_invalid", "storage_error", "internal_error"}
    return value if value in allowed else ("internal_error" if value else None)


def validate_snapshot(run: Run, config: OrchestrationConfig, binding: Binding):
    if type(run) is not Run or type(config) is not OrchestrationConfig or type(binding) is not Binding:
        raise CoordinationError("financial_snapshot_invalid")
    expected_config_digest = fingerprint({"orchestration": config.model_dump(mode="json"),
                                          "binding_digest": binding.fingerprint})
    if (run.config_digest != expected_config_digest
            or run.budget_units != rub_units(config.max_budget_rub)
            or run.request_cap > config.max_requests):
        raise CoordinationError("financial_snapshot_mismatch")
    config_json = canonical(config.model_dump(mode="json"))
    pricing_digest = fingerprint(config.pricing.model_dump(mode="json"))
    snapshot_digest = fingerprint({"version": 1, "run": asdict(run), "config": config.model_dump(mode="json"),
                                   "binding": asdict(binding), "pricing_digest": pricing_digest})
    return canonical(asdict(run)), config_json, canonical(asdict(binding)), pricing_digest, snapshot_digest
