"""Local-only persistence and adapters for the AI-first UX experiment.

This module is deliberately not used by the standard recognition pipeline.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any
from urllib.parse import urlsplit

from averon_import.services.ai_first.assembly import page_from_payload
from averon_import.services.ai_first.orchestration import (
    AdapterDescriptor, AdapterPageResult, OrchestrationConfig, PageExtractionRequest,
    RequestEstimate, TokenPricing,
)
from averon_import.services.ai_first.private_acl import (
    PrivateAclError, create_private_directory, verify_private_path, verify_private_tree,
)


_STORE_LOCKS: dict[str, threading.RLock] = {}
_STORE_LOCKS_GUARD = threading.Lock()


def _store_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _STORE_LOCKS_GUARD:
        return _STORE_LOCKS.setdefault(key, threading.RLock())


class AIWorkspaceStore:
    """Strictly private per-document state store with crash-recoverable writes."""

    _NAMES = {"transcription.json", "review.json", "report.json", "progress.json", "editor-draft.json", "active-run.json", "access.json"}
    _TRANSACTION_NAME = ".state-transaction.json"
    _MAX_TRANSACTION_BYTES = 64 * 1024 * 1024

    def __init__(self, document_root: str | os.PathLike[str]):
        self.root = Path(document_root) / "ai_first"
        self._lock = _store_lock(self.root)

    def ensure(self) -> Path:
        try:
            if self.root.exists():
                verify_private_tree(self.root)
            else:
                create_private_directory(self.root)
            return self.root
        except (OSError, PrivateAclError):
            raise RuntimeError("ai_first_private_storage_unverified") from None

    @property
    def checkpoint_directory(self) -> Path:
        return self.ensure() / "checkpoint"

    def select_run(self, document_id: str, *, new_run: bool = False) -> tuple[str, Path]:
        """Keep legacy checkpoints in place; new runs get distinct private directories."""
        active = self.read("active-run.json")
        if not new_run:
            if active is None:
                return f"local-{document_id}", self.checkpoint_directory
            run_id = active.get("run_id", "")
            if not re.fullmatch(r"run-[0-9a-f]{32}", run_id):
                raise RuntimeError("ai_first_private_state_unavailable")
            return run_id, self.ensure() / "runs" / run_id / "checkpoint"
        from uuid import uuid4
        run_id = f"run-{uuid4().hex}"
        root = self.ensure() / "runs"
        if root.exists():
            verify_private_tree(root)
        else:
            create_private_directory(root)
        run_root = create_private_directory(root / run_id)
        # A private atomic JSON snapshot preserves the previous OCR, ledger and
        # buffered edits. Its checkpoint is never moved or removed.
        snapshot = AIWorkspaceStore(run_root)
        for name in self._NAMES - {"active-run.json"}:
            previous = self.read(name)
            if previous is not None:
                snapshot.write(name, previous)
        self.write("active-run.json", {"version": 1, "run_id": run_id})
        return run_id, run_root / "checkpoint"

    def read(self, name: str) -> Any | None:
        if name not in self._NAMES:
            raise ValueError("invalid_ai_first_store_file")
        with self._lock:
            root = self.ensure()
            self._recover_transaction_locked(root)
            path = root / name
            if not path.exists():
                return None
            try:
                verify_private_path(path, directory=False)
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, PrivateAclError):
                raise RuntimeError("ai_first_private_state_unavailable") from None

    def write(self, name: str, payload: Any) -> None:
        if name not in self._NAMES:
            raise ValueError("invalid_ai_first_store_file")
        with self._lock:
            root = self.ensure()
            self._recover_transaction_locked(root)
            self._write_atomic_json(root, name, payload)

    @staticmethod
    def _encoded(payload: Any) -> bytes:
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")

    def _write_atomic_json(self, root: Path, name: str, payload: Any) -> None:
        temporary: str | None = None
        try:
            encoded = self._encoded(payload)
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{name}.", suffix=".tmp", dir=root, delete=False,
            ) as stream:
                temporary = stream.name
                if os.name != "nt":
                    os.chmod(temporary, 0o600)
                verify_private_path(temporary, directory=False)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, root / name)
            temporary = None
            verify_private_path(root / name, directory=False)
            verify_private_tree(root)
        except (OSError, ValueError, PrivateAclError):
            raise RuntimeError("ai_first_private_state_unavailable") from None
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)

    def write_many(self, payloads: dict[str, Any]) -> None:
        """Commit related JSON files as one recoverable document-state transaction.

        The private journal is written atomically before any target changes. If
        the process stops mid-publication, the next read reapplies the same
        complete bundle. This is process-local serialized; deployment still
        requires the documented single-worker configuration.
        """
        if not isinstance(payloads, dict) or not payloads or not set(payloads) <= self._NAMES:
            raise ValueError("invalid_ai_first_store_transaction")
        with self._lock:
            root = self.ensure()
            self._recover_transaction_locked(root)
            transaction_body = {"version": 1, "writes": payloads}
            encoded = self._encoded(transaction_body)
            if len(encoded) > self._MAX_TRANSACTION_BYTES:
                raise RuntimeError("ai_first_private_state_unavailable")
            envelope = {**transaction_body, "sha256": hashlib.sha256(encoded).hexdigest()}
            self._write_atomic_json(root, self._TRANSACTION_NAME, envelope)
            self._apply_transaction_locked(root, envelope)

    def _recover_transaction_locked(self, root: Path) -> None:
        journal = root / self._TRANSACTION_NAME
        if not journal.exists():
            return
        try:
            verify_private_path(journal, directory=False)
            payload = json.loads(journal.read_text(encoding="utf-8"))
            self._validate_transaction(payload)
        except (OSError, ValueError, PrivateAclError):
            raise RuntimeError("ai_first_private_state_unavailable") from None
        self._apply_transaction_locked(root, payload)

    def _validate_transaction(self, payload: Any) -> None:
        if (not isinstance(payload, dict) or set(payload) != {"version", "writes", "sha256"}
                or payload.get("version") != 1 or not isinstance(payload.get("writes"), dict)
                or not payload["writes"] or not set(payload["writes"]) <= self._NAMES
                or not isinstance(payload.get("sha256"), str)):
            raise ValueError("invalid_ai_first_store_transaction")
        body = {"version": 1, "writes": payload["writes"]}
        encoded = self._encoded(body)
        if (len(encoded) > self._MAX_TRANSACTION_BYTES
                or not hashlib.sha256(encoded).hexdigest() == payload["sha256"]):
            raise ValueError("invalid_ai_first_store_transaction")

    def _apply_transaction_locked(self, root: Path, payload: dict[str, Any]) -> None:
        try:
            for name, value in payload["writes"].items():
                self._write_atomic_json(root, name, value)
            journal = root / self._TRANSACTION_NAME
            verify_private_path(journal, directory=False)
            journal.unlink()
            verify_private_tree(root)
        except (OSError, ValueError, PrivateAclError):
            raise RuntimeError("ai_first_private_state_unavailable") from None


class SyntheticPageAdapter:
    """Deterministic fake OCR. It never reads the PDF image or performs I/O."""

    def __init__(self, user_context: str | None = None):
        context = user_context or ""
        self._scope = hashlib.sha256(
            b"averon-ai-first-fake-context-v1\0" + context.encode("utf-8"),
        ).hexdigest()
        self._descriptor = AdapterDescriptor(
            provider_id="offline_synthetic",
            model_id="fake_vision",
            model_version="synthetic-v1",
            prompt_version="none",
            schema_version="vision-page-synthetic-v1",
            parameters={"user_context_supplied": bool(context)},
        )
        self.max_requests = 10000
        self.max_pages = 10000
        self.rate_limits = None
        self.pricing_profile_id = None

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    @property
    def private_checkpoint_scope_fingerprint(self) -> str:
        return self._scope

    def estimate(self, image) -> RequestEstimate:
        return RequestEstimate(reserved_input_tokens=0, reserved_output_tokens=1)

    async def extract(self, request: PageExtractionRequest) -> AdapterPageResult:
        if request.cancel_event is not None and request.cancel_event.is_set():
            from averon_import.services.ai_first.orchestration import AdapterRequestError
            raise AdapterRequestError(
                "cancelled_before_dispatch", outcome_unknown=False, request_started=False,
            )
        page = request.page_number
        payload = {
            "page": page,
            "tables": [{
                "headers": ["Поз.", "Наименование", "Артикул", "Ед. изм.", "Количество", "Примечание"],
                "rows": [
                    {"kind": "item", "cells": [
                        {"column_index": 0, "raw_text": "1"},
                        {"column_index": 1, "raw_text": "Демо-позиция A"},
                        {"column_index": 2, "raw_text": "DEMO-A"},
                        {"column_index": 3, "raw_text": "шт."},
                        {"column_index": 4, "raw_text": "2"},
                        {"column_index": 5, "raw_text": "Синтетический пример"},
                    ]},
                    {"kind": "item_candidate", "cells": [
                        {"column_index": 0, "raw_text": "2"},
                        {"column_index": 1, "raw_text": "Демо-позиция B"},
                        {"column_index": 2, "raw_text": "DEMO-B"},
                        {"column_index": 3, "raw_text": "компл."},
                        {"column_index": 4, "raw_text": "1", "alternatives": ["7"], "unreadable": True},
                        {"column_index": 5, "raw_text": "Требует проверки"},
                    ]},
                ],
            }],
        }
        extraction = page_from_payload(
            payload, source_document_id=request.source_document_id, page_number=page,
        )
        await asyncio.sleep(0)
        return AdapterPageResult(extraction=extraction)


class AICancellationSignal:
    """Thread-safe bridge from the HTTP cancellation route to asyncio."""

    def __init__(self):
        self._requested = threading.Event()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None

    def bind(self, loop: asyncio.AbstractEventLoop, event: asyncio.Event) -> None:
        with self._lock:
            self._loop = loop
            self._event = event
            requested = self._requested.is_set()
        if requested:
            event.set()

    def request(self) -> None:
        self._requested.set()
        with self._lock:
            loop, event = self._loop, self._event
        if loop is not None and event is not None and not loop.is_closed():
            loop.call_soon_threadsafe(event.set)


def _env_int(environ: dict[str, str], name: str, *, minimum: int = 1, maximum: int = 100_000_000) -> int:
    raw = environ.get(name, "").strip()
    if not re.fullmatch(r"[0-9]+", raw):
        raise ValueError("live_configuration_missing_or_invalid")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ValueError("live_configuration_missing_or_invalid")
    return value


def _env_decimal(environ: dict[str, str], name: str) -> Decimal:
    try:
        value = Decimal(environ.get(name, "").strip())
    except (InvalidOperation, ValueError):
        raise ValueError("live_configuration_missing_or_invalid") from None
    if not value.is_finite() or value <= 0:
        raise ValueError("live_configuration_missing_or_invalid")
    return value


def build_live_qwen_runtime(
    *, settings: Any, secret_store: Any, quota_registry: Any,
    user_context: str | None, page_count: int, environ: dict[str, str] | None = None,
):
    """Build an explicitly enabled local Qwen runtime from operator settings.

    Every token, request and budget cap is operator configured. They are local
    guards, not claims about Yandex account quotas.
    """
    from averon_import.services.ai_first.qwen_vision_adapter import QwenVisionAdapter
    from averon_import.services.ocr.qwen_vision_client import QwenVisionConfig, VisionLimits

    env = dict(os.environ if environ is None else environ)
    rpm = _env_int(env, "AVERON_AI_FIRST_RPM_LIMIT")
    tpm = _env_int(env, "AVERON_AI_FIRST_TPM_LIMIT")
    input_bound = _env_int(env, "AVERON_AI_FIRST_INPUT_TOKEN_UPPER_BOUND")
    max_requests = _env_int(env, "AVERON_AI_FIRST_MAX_REQUESTS", maximum=10000)
    max_budget = _env_decimal(env, "AVERON_AI_FIRST_MAX_BUDGET_RUB")
    input_rate = _env_decimal(env, "AVERON_AI_FIRST_INPUT_RUB_PER_1K")
    output_rate = _env_decimal(env, "AVERON_AI_FIRST_OUTPUT_RUB_PER_1K")
    cached_raw = env.get("AVERON_AI_FIRST_CACHED_INPUT_RUB_PER_1K", "").strip()
    cached_rate = _env_decimal(env, "AVERON_AI_FIRST_CACHED_INPUT_RUB_PER_1K") if cached_raw else None
    output_cap = _env_int(env, "AVERON_AI_FIRST_MAX_OUTPUT_TOKENS", maximum=32768)
    dpi = _env_int(env, "AVERON_AI_FIRST_DPI", maximum=600)
    timeout = _env_int(env, "AVERON_AI_FIRST_REQUEST_TIMEOUT_S", maximum=300)
    pricing = TokenPricing(
        profile_id="operator-configured-local",
        input_rub_per_1k=input_rate,
        cached_input_rub_per_1k=cached_rate,
        output_rub_per_1k=output_rate,
    )
    config = OrchestrationConfig(
        concurrency=1,
        dpi=dpi,
        max_requests=max_requests,
        requests_per_minute=rpm,
        tokens_per_minute=tpm,
        max_budget_rub=max_budget,
        pricing=pricing,
        max_pages=max(1, min(page_count, 10000)),
    )
    limits = VisionLimits(
        max_output_tokens=output_cap,
        reasoning_effort="none",
        max_requests=max_requests,
        max_pages=max(1, min(page_count, 10000)),
        request_timeout_s=timeout,
        total_timeout_s=600.0,
        dpi=dpi,
    )
    adapter = QwenVisionAdapter.from_app_settings(
        settings, secret_store,
        limits=limits,
        quota_registry=quota_registry,
        requests_per_minute=rpm,
        tokens_per_minute=tpm,
        input_token_upper_bound=input_bound,
        pricing_profile_id="operator-configured-local",
        user_context=user_context,
        environ=env,
    )
    model_ok = bool(re.fullmatch(r"gpt://[^/\s]+/qwen3\.6-35b-a3b", adapter.client.config.model, re.IGNORECASE))
    parsed = urlsplit(adapter.client.config.base_url)
    endpoint_ok = (
        parsed.scheme == "https"
        and (parsed.hostname or "").casefold() == "ai.api.cloud.yandex.net"
        and parsed.path.rstrip("/") == "/v1"
        and not parsed.query and not parsed.fragment
    )
    if not adapter.client.config.configured or not model_ok or not endpoint_ok:
        raise ValueError("live_configuration_not_allowed")
    return adapter, config
