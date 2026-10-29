"""Qwen Vision bridge for the isolated AI-first page orchestrator.

The synchronous Vision client is always called in a worker thread. A quota
registry must be shared by all document jobs using the same credential in this
process. It stores only an in-memory HMAC key, never the credential itself.
"""
from __future__ import annotations

import asyncio
from collections import deque
import hashlib
import hmac
import json
import math
import re
import secrets as random_secrets
import threading
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from averon_import.services.ai_first.assembly import page_from_payload
from averon_import.services.ai_first.contracts import AIPageExtraction, AIReviewIssue
from averon_import.services.ai_first.orchestration import (
    AdapterDescriptor, AdapterPageResult, AdapterRequestError, PageExtractionRequest,
    PageImage, RequestEstimate, TokenUsage,
)
from averon_import.services.ocr.qwen_vision_client import (
    USER_CONTEXT_POLICY_VERSION, QwenVisionClient, QwenVisionConfig,
    QwenVisionError, VisionCompletion, VisionLimits, validate_user_context,
)
from averon_import.services.ocr.qwen_vision_contract import VisionPage
from averon_import.services.secrets import SecretStore


QWEN_VISION_PROMPT_VERSION = "qwen-vision-page-v1.4"
QWEN_VISION_CONTEXT_WINDOW_TOKENS = 262144


class _SharedQuota:
    def __init__(self, requests_per_minute: int, tokens_per_minute: int, window_s: float):
        self.requests_per_minute = requests_per_minute
        self.tokens_per_minute = tokens_per_minute
        self.window_s = window_s
        self._events: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()

    async def acquire(self, token_reservation: int, cancel_event: asyncio.Event | None) -> bool:
        if token_reservation > self.tokens_per_minute:
            return False
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return False
            with self._lock:
                now = time.monotonic()
                while self._events and self._events[0][0] <= now - self.window_s:
                    self._events.popleft()
                used_tokens = sum(tokens for _, tokens in self._events)
                if (
                    len(self._events) < self.requests_per_minute
                    and used_tokens + token_reservation <= self.tokens_per_minute
                ):
                    self._events.append((now, token_reservation))
                    return True
                if not self._events:
                    return False
                delay = max(0.001, self._events[0][0] + self.window_s - now)
            if cancel_event is None:
                await asyncio.sleep(delay)
                continue
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=delay)
                return False
            except asyncio.TimeoutError:
                continue


class QwenVisionQuotaRegistry:
    """Process-local shared rolling RPM/TPM limiter keyed by credential.

    Create one registry at application/job-composition scope and pass it to
    every Qwen adapter. The HMAC key uses a random per-process salt and is never
    included in descriptors, checkpoints, or logs.
    """

    def __init__(self, *, window_s: float = 60.0):
        if isinstance(window_s, bool) or not isinstance(window_s, (int, float)):
            raise ValueError("invalid quota window")
        if not math.isfinite(window_s) or window_s <= 0:
            raise ValueError("invalid quota window")
        self.window_s = float(window_s)
        self._salt = random_secrets.token_bytes(32)
        self._entries: dict[bytes, _SharedQuota] = {}
        self._lock = threading.Lock()

    def _get(
        self, credential: str, requests_per_minute: int, tokens_per_minute: int,
    ) -> _SharedQuota:
        key = hmac.new(self._salt, credential.encode("utf-8"), hashlib.sha256).digest()
        with self._lock:
            existing = self._entries.get(key)
            if existing is not None:
                if (
                    existing.requests_per_minute != requests_per_minute
                    or existing.tokens_per_minute != tokens_per_minute
                ):
                    raise ValueError("shared provider quota configuration mismatch")
                return existing
            quota = _SharedQuota(requests_per_minute, tokens_per_minute, self.window_s)
            self._entries[key] = quota
            return quota

    async def acquire(
        self,
        credential: str,
        requests_per_minute: int,
        tokens_per_minute: int,
        token_reservation: int,
        cancel_event: asyncio.Event | None,
    ) -> bool:
        quota = self._get(credential, requests_per_minute, tokens_per_minute)
        return await quota.acquire(token_reservation, cancel_event)


class QwenVisionAdapter:
    """Maps Qwen VisionPage results into V2A without semantic promotion."""

    def __init__(
        self,
        client: QwenVisionClient,
        *,
        quota_registry: QwenVisionQuotaRegistry,
        requests_per_minute: int,
        tokens_per_minute: int,
        input_token_upper_bound: int | None,
        pricing_profile_id: str,
        user_context: str | None = None,
    ):
        for label, value in (
            ("requests_per_minute", requests_per_minute),
            ("tokens_per_minute", tokens_per_minute),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"invalid {label}")
        if input_token_upper_bound is not None and (
            type(input_token_upper_bound) is not int or input_token_upper_bound <= 0
        ):
            raise ValueError("invalid input token upper bound")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,96}", pricing_profile_id):
            raise ValueError("invalid pricing profile")
        try:
            user_context = validate_user_context(user_context)
        except QwenVisionError as exc:
            raise ValueError(exc.code) from None

        self.client = client
        self.requests_per_minute = requests_per_minute
        self.tokens_per_minute = tokens_per_minute
        self.rate_limits = (requests_per_minute, tokens_per_minute)
        self.max_requests = client.config.limits.max_requests
        self.max_pages = client.config.limits.max_pages
        self.input_token_upper_bound = input_token_upper_bound
        self.pricing_profile_id = pricing_profile_id
        self._user_context = user_context
        self._quota_registry = quota_registry
        self._descriptor = self._build_descriptor()

    @classmethod
    def from_app_settings(
        cls,
        settings: Any,
        secret_store: SecretStore,
        *,
        limits: VisionLimits,
        quota_registry: QwenVisionQuotaRegistry,
        requests_per_minute: int,
        tokens_per_minute: int,
        input_token_upper_bound: int | None,
        pricing_profile_id: str,
        user_context: str | None = None,
        environ: Any = None,
        auth_scheme: str = "Api-Key",
        transport: Any = None,
    ) -> QwenVisionAdapter:
        config = QwenVisionConfig.from_app_settings(
            settings, secret_store, limits=limits, environ=environ,
            auth_scheme=auth_scheme,
        )
        client = QwenVisionClient(config, transport=transport)
        return cls(
            client,
            quota_registry=quota_registry,
            requests_per_minute=requests_per_minute,
            tokens_per_minute=tokens_per_minute,
            input_token_upper_bound=input_token_upper_bound,
            pricing_profile_id=pricing_profile_id,
            user_context=user_context,
        )

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    @property
    def private_checkpoint_scope_fingerprint(self) -> str:
        """Stable credential/account scope stored only in the private checkpoint."""
        scope_value = {
            "api_key": self.client.config.api_key,
            "auth_scheme": self.client.config.auth_scheme,
            "folder_id": self.client.config.folder_id,
        }
        scope = json.dumps(
            scope_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        if self._user_context is not None:
            context_binding = hmac.new(
                self.client.config.api_key.encode("utf-8"),
                b"averon-qwen-document-context-v1\0" + self._user_context.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            scope_value["document_context_policy_version"] = USER_CONTEXT_POLICY_VERSION
            scope_value["document_context_hmac"] = context_binding
            scope_value["document_context_template_sha256"] = self.client.contextual_request_template_sha256
            scope = json.dumps(
                scope_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
        return hashlib.sha256(b"averon-qwen-checkpoint-scope-v1\0" + scope).hexdigest()

    def _build_descriptor(self) -> AdapterDescriptor:
        config = self.client.config
        limits = config.limits
        model_version = config.model.rsplit("/", 1)[-1]
        endpoint = urlsplit(config.base_url + "/chat/completions")
        hostname = (endpoint.hostname or "").casefold()
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = endpoint.port
        authority = hostname if port in (None, 443) else f"{hostname}:{port}"
        normalized_endpoint = urlunsplit((
            endpoint.scheme.casefold(), authority, endpoint.path or "/", "", "",
        ))
        schema = json.dumps(
            VisionPage.model_json_schema(),
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        schema_version = hashlib.sha256(schema).hexdigest()[:16]
        return AdapterDescriptor(
            provider_id="yandex_ai_studio",
            model_id="qwen_vision",
            model_version=model_version,
            prompt_version=QWEN_VISION_PROMPT_VERSION,
            schema_version=f"vision-page-{schema_version}",
            parameters={
                "input_token_upper_bound": self.input_token_upper_bound,
                "output_token_limit": limits.max_output_tokens,
                "reasoning_effort": limits.reasoning_effort,
                "request_timeout_s": limits.request_timeout_s,
                "total_timeout_s": limits.total_timeout_s,
                "dpi": limits.dpi,
                "max_image_bytes": limits.max_image_bytes,
                "max_image_pixels": limits.max_image_pixels,
                "max_request_bytes": limits.max_request_bytes,
                "max_response_bytes": limits.max_response_bytes,
                "max_requests": limits.max_requests,
                "max_pages": limits.max_pages,
                "max_tables": limits.max_tables,
                "max_rows_per_page": limits.max_rows_per_page,
                "max_columns_per_table": limits.max_columns_per_table,
                "max_cells_per_page": limits.max_cells_per_page,
                "pricing_profile_id": self.pricing_profile_id,
                "model_deployment_sha256": "sha256:" + hashlib.sha256(
                    config.model.encode("utf-8"),
                ).hexdigest(),
                "effective_endpoint_sha256": "sha256:" + hashlib.sha256(
                    normalized_endpoint.encode("utf-8"),
                ).hexdigest(),
                "prompt_sha256": "sha256:" + self.client.prompt_sha256,
                "auth_scheme": config.auth_scheme,
            },
        )

    def estimate(self, image: PageImage) -> RequestEstimate:
        if not self.client.config.configured:
            raise ValueError("qwen vision is not configured")
        if self.input_token_upper_bound is None:
            raise ValueError("conservative input token estimate is unavailable")
        limits = self.client.config.limits
        if image.content_type != "image/png" or image.width * image.height > limits.max_image_pixels:
            raise ValueError("qwen image constraints exceeded")
        # Build the exact body here so context-specific budgeting uses the
        # same serialization; the client enforces its byte cap immediately
        # before dispatch, and the pilot runner reports its own preflight size.
        self.client.measure_request_bytes(
            image.data, image.page_number, user_context=self._user_context,
        )
        output_bound = limits.max_output_tokens
        context_token_reservation = self.client.additional_context_token_reservation(
            image.data, image.page_number, self._user_context,
        )
        input_bound = self.input_token_upper_bound + context_token_reservation
        if input_bound + output_bound > QWEN_VISION_CONTEXT_WINDOW_TOKENS:
            raise ValueError("request token reservation exceeds model context window")
        if input_bound + output_bound > self.tokens_per_minute:
            raise ValueError("request token reservation exceeds provider TPM")
        return RequestEstimate(
            reserved_input_tokens=input_bound,
            reserved_output_tokens=output_bound,
        )

    async def extract(self, request: PageExtractionRequest) -> AdapterPageResult:
        cancel_event = request.cancel_event
        if cancel_event is not None and cancel_event.is_set():
            raise AdapterRequestError(
                "cancelled_before_dispatch", outcome_unknown=False, request_started=False,
            )
        estimate = self.estimate(request.image)
        token_reservation = estimate.reserved_input_tokens + estimate.reserved_output_tokens
        try:
            admitted = await self._quota_registry.acquire(
                self.client.config.api_key,
                self.requests_per_minute,
                self.tokens_per_minute,
                token_reservation,
                cancel_event,
            )
        except asyncio.CancelledError:
            raise AdapterRequestError(
                "cancelled_before_dispatch", outcome_unknown=False, request_started=False,
            ) from None
        if not admitted:
            code = "cancelled_before_dispatch" if cancel_event is not None and cancel_event.is_set() else "provider_quota_unavailable"
            raise AdapterRequestError(code, outcome_unknown=False, request_started=False)

        thread_cancel = threading.Event()
        call_task = asyncio.create_task(asyncio.to_thread(
            self.client.extract,
            request.image.data,
            request.page_number,
            timeout_s=self.client.config.limits.request_timeout_s,
            cancel=thread_cancel,
            user_context=self._user_context,
        ))
        try:
            completion = await asyncio.shield(call_task)
        except asyncio.CancelledError:
            thread_cancel.set()
            try:
                completion = await self._wait_for_thread(call_task, thread_cancel)
            except QwenVisionError as exc:
                return self._raise_client_error(exc, request)
        except QwenVisionError as exc:
            return self._raise_client_error(exc, request)
        return self._convert_completion(completion, request)

    @staticmethod
    async def _wait_for_thread(task: asyncio.Task, cancel: threading.Event) -> VisionCompletion:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancel.set()
        return task.result()

    def _raise_client_error(self, exc: QwenVisionError, request: PageExtractionRequest) -> AdapterPageResult:
        diagnostics = exc.diagnostics
        usage = self._usage(diagnostics.get("usage"))
        http_status = diagnostics.get("http_status")
        http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        finish_reason = diagnostics.get("finish_reason")
        finish_reason = finish_reason if isinstance(finish_reason, str) else None
        request_latency_ms = diagnostics.get("latency_ms")
        request_latency_ms = (
            float(request_latency_ms)
            if isinstance(request_latency_ms, (int, float))
            and not isinstance(request_latency_ms, bool)
            and math.isfinite(request_latency_ms)
            and request_latency_ms >= 0
            else None
        )
        if exc.code == "incomplete_response":
            return AdapterPageResult(
                extraction=self._empty_incomplete_page(request, "incomplete_response"),
                usage=usage,
                http_status=http_status,
                finish_reason=finish_reason,
                request_latency_ms=request_latency_ms,
            )
        request_started = diagnostics.get("request_attempted") is True
        status = diagnostics.get("http_status")
        if exc.code == "http_error" and type(status) is int:
            safe_code = f"http_{status}" if 400 <= status < 600 else "http_error"
        else:
            safe_code = exc.code
        status_unknown = type(status) is int and 500 <= status < 600
        outcome_unknown = request_started and (
            exc.code in {"timeout", "transport_error", "cancelled"} or status_unknown
        )
        raise AdapterRequestError(
            safe_code,
            outcome_unknown=outcome_unknown,
            usage=usage,
            request_started=request_started,
            http_status=http_status,
            finish_reason=finish_reason,
            request_latency_ms=request_latency_ms,
        ) from None

    @staticmethod
    def _usage(value: Any) -> TokenUsage | None:
        if not isinstance(value, dict):
            return None
        details_in = value.get("prompt_tokens_details")
        details_out = value.get("completion_tokens_details")
        input_tokens = value.get("prompt_tokens")
        output_tokens = value.get("completion_tokens")
        cached_tokens = details_in.get("cached_tokens") if isinstance(details_in, dict) else None
        reasoning_tokens = details_out.get("reasoning_tokens") if isinstance(details_out, dict) else None
        input_tokens = input_tokens if type(input_tokens) is int and input_tokens >= 0 else None
        output_tokens = output_tokens if type(output_tokens) is int and output_tokens >= 0 else None
        cached_tokens = cached_tokens if type(cached_tokens) is int and cached_tokens >= 0 else None
        reasoning_tokens = reasoning_tokens if type(reasoning_tokens) is int and reasoning_tokens >= 0 else None
        if input_tokens is not None and cached_tokens is not None and cached_tokens > input_tokens:
            cached_tokens = None
        if output_tokens is not None and reasoning_tokens is not None and reasoning_tokens > output_tokens:
            reasoning_tokens = None
        if all(metric is None for metric in (input_tokens, output_tokens, cached_tokens, reasoning_tokens)):
            return None
        try:
            result = TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cached_tokens,
                reasoning_tokens=reasoning_tokens,
            )
        except (TypeError, ValueError):
            return None
        if all(getattr(result, name) is None for name in (
            "input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens",
        )):
            return None
        return result

    @staticmethod
    def _convert_completion(completion: VisionCompletion, request: PageExtractionRequest) -> AdapterPageResult:
        extraction = page_from_payload(
            completion.page.model_dump(mode="json"),
            source_document_id=request.source_document_id,
            page_number=request.page_number,
        )
        has_unreadable = bool(completion.page.unreadable_fragments) or any(
            cell.unreadable
            for table in completion.page.tables
            for row in table.rows
            for cell in row.cells
        )
        if has_unreadable:
            issue_id = QwenVisionAdapter._incomplete_issue_id(request.source_document_id, request.page_number)
            issue = AIReviewIssue(
                id=issue_id,
                source_document_id=request.source_document_id,
                code="transcription_incomplete",
                severity="blocking",
                page_number=request.page_number,
                record_ids=[],
                detail="The page transcription contains unreadable source content.",
            )
            extraction = extraction.model_copy(update={
                "status": "incomplete", "issues": [*extraction.issues, issue],
            })
        return AdapterPageResult(
            extraction=extraction,
            usage=QwenVisionAdapter._usage(completion.diagnostics.get("usage")),
            http_status=(
                completion.diagnostics.get("http_status")
                if type(completion.diagnostics.get("http_status")) is int
                and 100 <= completion.diagnostics["http_status"] <= 599 else None
            ),
            finish_reason=(
                completion.diagnostics.get("finish_reason")
                if completion.diagnostics.get("finish_reason") in (
                    "stop", "length", "content_filter", "tool_calls", "unknown",
                ) else None
            ),
            request_latency_ms=(
                float(completion.diagnostics["latency_ms"])
                if isinstance(completion.diagnostics.get("latency_ms"), (int, float))
                and not isinstance(completion.diagnostics.get("latency_ms"), bool)
                and math.isfinite(completion.diagnostics["latency_ms"])
                and completion.diagnostics["latency_ms"] >= 0 else None
            ),
        )

    @staticmethod
    def _empty_incomplete_page(request: PageExtractionRequest, reason: str) -> AIPageExtraction:
        extraction = page_from_payload({
            "page": request.page_number,
            "tables": [],
            "unreadable_fragments": [],
            "handwritten_replacements": [],
        }, source_document_id=request.source_document_id)
        issue = AIReviewIssue(
            id=QwenVisionAdapter._incomplete_issue_id(request.source_document_id, request.page_number),
            source_document_id=request.source_document_id,
            code="transcription_incomplete",
            severity="blocking",
            page_number=request.page_number,
            record_ids=[],
            detail="The model response ended before a complete page transcription was available.",
        )
        # `reason` is an internal fixed code; no response text is retained here.
        if reason != "incomplete_response":
            raise ValueError("unsupported incomplete reason")
        return extraction.model_copy(update={"status": "incomplete", "issues": [issue]})

    @staticmethod
    def _incomplete_issue_id(source_document_id: str, page_number: int) -> str:
        digest = hashlib.sha256(f"{source_document_id}:{page_number}:transcription_incomplete".encode()).hexdigest()
        return f"issue:{digest[:20]}"
