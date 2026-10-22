"""One-page, offline-first Qwen Vision pilot runner.

This entry point is intentionally isolated from the application runtime. A live
request requires both ``--allow-paid-api`` and ``--confirm-live-preflight``.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from typing import Any, Callable
from urllib.parse import urlsplit
import uuid

from averon_import.services.ai_first.orchestration import (
    CheckpointStore, FitZPageRenderer, OrchestrationConfig, OrchestrationError,
    PageOrchestrator, TokenPricing, inspect_pdf,
)
from averon_import.services.ai_first.qwen_vision_adapter import (
    QwenVisionAdapter, QwenVisionQuotaRegistry,
)
from averon_import.services.ocr.qwen_vision_client import (
    MAX_OUTPUT_TOKENS_CAP, QwenVisionConfig, VisionLimits,
)
from averon_import.services.secrets import create_secret_store
from averon_import.services.app_settings import AppSettingsService
from averon_import.tools.qwen_vision_pilot import (
    _default_data_dir as _default_data_dir_v12,
    _default_output_root as _default_output_root_v12,
    _identity_sid,
    _max_output_tokens,
    _request_timeout_s,
    _secure_run_directory,
    _write_json_private,
)


PILOT_RETENTION_DAYS = 7
PILOT_COOLDOWN_SECONDS = 360
MAX_PILOT_DPI = 300
_ALLOWED_MODEL = re.compile(r"^gpt://[^/\s]+/qwen3\.6-35b-a3b/latest$", re.IGNORECASE)
_PILOT_QUOTA_REGISTRY = QwenVisionQuotaRegistry()


class ControlledPilotError(RuntimeError):
    def __init__(self, code: str):
        self.code = code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "pilot_error"
        super().__init__(self.code)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if str(parsed) != value.strip() or parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _positive_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a finite positive decimal") from None
    if not parsed.is_finite() or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive decimal")
    return parsed


def _default_output_root() -> Path:
    return _default_output_root_v12() / "controlled"


def _pilot_guard_state_dir() -> Path:
    """Stable per-user location, independent of a custom --data-dir override."""
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        root = Path(local) if local else Path.home() / "AppData" / "Local"
        return (root / "Averon Import" / "data").resolve()
    state = os.environ.get("XDG_STATE_HOME", "").strip()
    root = Path(state) if state else Path.home() / ".local" / "state"
    return (root / "averon-import" / "data").resolve()


def _pricing_profile_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,96}", value):
        raise argparse.ArgumentTypeError("use a short non-secret pricing profile identifier")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a single-page Qwen Vision pilot; defaults to offline preflight",
    )
    parser.add_argument("--pdf", type=Path, required=True, help="Local PDF path")
    parser.add_argument("--page", type=_positive_int, required=True, help="Exactly one 1-based PDF page")
    parser.add_argument("--dpi", type=_positive_int, default=150, help=f"Render DPI (36..{MAX_PILOT_DPI})")
    parser.add_argument(
        "--max-output-tokens", type=_max_output_tokens, default=MAX_OUTPUT_TOKENS_CAP,
        help=f"Maximum generated output tokens (1..{MAX_OUTPUT_TOKENS_CAP})",
    )
    parser.add_argument(
        "--request-timeout-s", type=_request_timeout_s, default=300.0,
        help="Per-request timeout (up to 300 seconds)",
    )
    parser.add_argument("--disable-reasoning", action="store_true")
    parser.add_argument("--requests-per-minute", type=_positive_int, required=True)
    parser.add_argument("--tokens-per-minute", type=_positive_int, required=True)
    parser.add_argument("--input-token-upper-bound", type=_positive_int, required=True)
    parser.add_argument("--input-rub-per-1k", type=_positive_decimal, required=True)
    parser.add_argument("--cached-input-rub-per-1k", type=_positive_decimal, required=True)
    parser.add_argument("--output-rub-per-1k", type=_positive_decimal, required=True)
    parser.add_argument("--pricing-profile-id", type=_pricing_profile_id, required=True)
    parser.add_argument("--budget-rub", type=_positive_decimal, required=True)
    parser.add_argument("--data-dir", type=Path, default=_default_data_dir_v12())
    parser.add_argument("--output-dir", type=Path, default=_default_output_root())
    parser.add_argument(
        "--allow-paid-api", action="store_true",
        help="Explicitly permit the one selected-page HTTP request after preflight",
    )
    parser.add_argument(
        "--confirm-live-preflight", action="store_true",
        help="Confirm current RPM/TPM, tariffs and input-token upper bound were verified",
    )
    return parser


def _allowed_model_and_endpoint(config: QwenVisionConfig) -> tuple[bool, bool]:
    model_ok = bool(_ALLOWED_MODEL.fullmatch(config.model.strip()))
    try:
        endpoint = urlsplit(config.base_url)
        endpoint_ok = (
            endpoint.scheme.casefold() == "https"
            and (endpoint.hostname or "").casefold() == "ai.api.cloud.yandex.net"
            and endpoint.path.rstrip("/") == "/v1"
            and endpoint.port in (None, 443)
            and endpoint.username is None
            and endpoint.password is None
            and not endpoint.query
            and not endpoint.fragment
        )
    except ValueError:
        endpoint_ok = False
    return model_ok, endpoint_ok


def _safe_cost(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value.quantize(Decimal("0.000001")))


def _summarize_structure(extraction: Any) -> dict[str, Any] | None:
    if extraction is None:
        return None
    rows = [row for table in extraction.tables for row in table.rows]
    cells = [cell for row in rows for cell in row.raw_cells]
    handwriting_count = sum(
        len(cell.handwritten_replacements or ()) for cell in cells
    ) + sum(len(row.handwritten_replacements or ()) for row in rows) + len(extraction.page_handwriting)
    return {
        "extraction_status": extraction.status,
        "table_count": len(extraction.tables),
        "source_row_count": len(rows),
        "cell_count": len(cells),
        "records_requiring_review_count": len(rows),
        "review_count_basis": "Every model row remains unverified and requires human review.",
        "unreadable_cell_count": sum(cell.unreadable is True for cell in cells),
        "unreadable_fragment_count": len(extraction.unreadable_fragments),
        "struck_through_row_count": sum(row.struck_through is True for row in rows),
        "handwriting_replacement_count": handwriting_count,
        "issue_count": len(extraction.issues),
        "response_schema_accepted": extraction.status in {"extracted", "no_specification"},
        "completeness_verified": False,
        "correctness_verified": False,
    }


class _LiveInvocationGuard:
    """Cross-process single-run lock plus conservative one-minute launch cooldown."""

    def __init__(self, state_dir: Path):
        self.directory = state_dir.expanduser().resolve() / "qwen-vision-controlled-pilot"
        self.path = self.directory / "quota.lock"
        self.stream: Any = None

    def __enter__(self) -> _LiveInvocationGuard:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            if self.directory.is_symlink():
                raise ControlledPilotError("pilot_lock_permissions_failed")
            if os.name == "nt":
                try:
                    sid = _identity_sid()
                    subprocess.run(
                        [
                            "icacls.exe", str(self.directory), "/inheritance:r", "/grant:r",
                            f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F",
                            "*S-1-5-32-544:(OI)(CI)F",
                        ],
                        check=True, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", stdin=subprocess.DEVNULL, timeout=15,
                    )
                except Exception:
                    raise ControlledPilotError("pilot_lock_permissions_failed") from None
            else:
                os.chmod(self.directory, 0o700)
                if self.directory.stat().st_mode & 0o077:
                    raise ControlledPilotError("pilot_lock_permissions_failed")
            if self.path.is_symlink():
                raise ControlledPilotError("pilot_lock_permissions_failed")
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            self.stream = os.fdopen(fd, "r+b", buffering=0)
            if os.name != "nt":
                os.chmod(self.path, 0o600)
                if self.path.stat().st_mode & 0o077:
                    raise ControlledPilotError("pilot_lock_permissions_failed")

            self.stream.seek(0, os.SEEK_END)
            if self.stream.tell() == 0:
                self.stream.write(b"\0")
                self.stream.flush()
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise ControlledPilotError("pilot_already_running") from None
            else:
                import fcntl
                try:
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise ControlledPilotError("pilot_already_running") from None
            self._check_cooldown()
            return self
        except ControlledPilotError:
            self.__exit__(None, None, None)
            raise
        except Exception:
            self.__exit__(None, None, None)
            raise ControlledPilotError("pilot_lock_unavailable") from None

    def _check_cooldown(self) -> None:
        assert self.stream is not None
        self.stream.seek(1)
        raw = self.stream.read(2048)
        if not raw:
            return
        try:
            stored = json.loads(raw.decode("utf-8"))
            last_started = datetime.fromisoformat(stored["last_live_start_utc"])
            if last_started.tzinfo is None:
                raise ValueError
        except (UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            raise ControlledPilotError("pilot_cooldown_state_invalid") from None
        age = (datetime.now(timezone.utc) - last_started.astimezone(timezone.utc)).total_seconds()
        if age < PILOT_COOLDOWN_SECONDS:
            raise ControlledPilotError("pilot_cooldown_active")

    def mark_live_start(self) -> None:
        assert self.stream is not None
        payload = json.dumps({
            "last_live_start_utc": datetime.now(timezone.utc).isoformat(),
            "cooldown_seconds": PILOT_COOLDOWN_SECONDS,
        }, separators=(",", ":")).encode("utf-8")
        self.stream.truncate(1)
        self.stream.seek(1)
        self.stream.write(payload)
        self.stream.flush()
        os.fsync(self.stream.fileno())

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        if self.stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        self.stream.close()
        self.stream = None


async def _execute_one_page(orchestrator: PageOrchestrator, pdf: Path, page: int,
                            cancel_event: asyncio.Event) -> Any:
    return await orchestrator.run(
        pdf, cancel_event=cancel_event, selected_pages={page}, retry_pages=None,
    )


def _report_from_orchestration(report: Any, page_number: int) -> dict[str, Any]:
    record = next((page for page in report.pages if page.page_number == page_number), None)
    if record is None:
        raise ControlledPilotError("selected_page_result_missing")
    attempt = record.attempts[-1] if record.attempts else None
    usage = attempt.usage if attempt is not None else None
    prompt_tokens = usage.input_tokens if usage is not None else None
    completion_tokens = usage.output_tokens if usage is not None else None
    reasoning_tokens = usage.reasoning_tokens if usage is not None else None
    safe_code = attempt.safe_error_code if attempt is not None else None
    http_status = attempt.http_status if attempt is not None else None
    if isinstance(safe_code, str) and safe_code.startswith("http_"):
        status_text = safe_code[5:]
        if http_status is None and status_text.isdigit():
            http_status = int(status_text)
    finish_reason = attempt.finish_reason if attempt is not None else None
    request_latency_ms = attempt.request_latency_ms if attempt is not None else None
    outcome = {
        "page_state": record.state,
        "request_attempted": bool(attempt and attempt.request_started),
        "safe_error_code": safe_code,
        "http_status": http_status if http_status is not None else None,
        "http_status_class": (
            f"{http_status // 100}xx" if http_status is not None
            else "2xx" if record.state == "completed"
            else None
        ),
        "finish_reason": finish_reason,
        "attempt_count": len(record.attempts),
        "request_latency_ms": request_latency_ms,
        "page_elapsed_ms": round(attempt.elapsed_s * 1000, 3) if attempt is not None else None,
    }
    actual_cost = attempt.actual_cost_rub if attempt is not None else None
    reserved_possible_cost = (
        attempt.reserved_cost_rub
        if attempt is not None and attempt.request_started and actual_cost is None
        else Decimal("0")
    )
    extraction = record.extraction
    if report.status == "cancelled":
        pilot_status = "cancelled"
    elif record.state == "unknown_outcome":
        pilot_status = "unknown_outcome"
    elif record.state == "completed" and report.source_integrity_verified:
        pilot_status = "completed"
    elif record.state == "incomplete":
        pilot_status = "completed_with_errors"
    else:
        pilot_status = "completed_with_errors"
    return {
        "status": pilot_status,
        "orchestration_status": report.status,
        "source_integrity_verified": report.source_integrity_verified,
        "selected_page": page_number,
        "request_outcome": outcome,
        "actual_usage": {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "reasoning_tokens": reasoning_tokens,
            "total_tokens": (
                prompt_tokens + completion_tokens
                if prompt_tokens is not None and completion_tokens is not None else None
            ),
        },
        "estimated_cost_rub": _safe_cost(actual_cost),
        "reserved_possible_cost_rub": _safe_cost(reserved_possible_cost),
        "structure": _summarize_structure(extraction),
        "_private_extraction": extraction,
    }


def run(
    args: argparse.Namespace,
    *,
    settings_service_factory: Callable[..., Any] | None = None,
    secret_store_factory: Callable[..., Any] | None = None,
    output_directory_factory: Callable[[Path], Path] | None = None,
    transport: Any = None,
    quota_registry: QwenVisionQuotaRegistry | None = None,
    cancel_event: asyncio.Event | None = None,
    live_guard_factory: Callable[[Path], Any] | None = None,
) -> tuple[int, Path, dict[str, Any]]:
    output_factory = output_directory_factory or _secure_run_directory
    try:
        output_root = args.output_dir.expanduser().resolve()
        repository_root = Path(__file__).resolve().parents[2]
        try:
            output_root.relative_to(repository_root)
        except ValueError:
            pass
        else:
            raise ControlledPilotError("pilot_output_must_be_outside_repository")
        run_dir = output_factory(output_root)
    except ControlledPilotError:
        raise
    except Exception:
        raise ControlledPilotError("output_directory_unavailable") from None

    started = time.perf_counter()
    run_id = uuid.uuid4().hex
    cancel_event = cancel_event or asyncio.Event()
    report: dict[str, Any] = {
        "format": "averon-qwen-vision-controlled-pilot-report-v1",
        "run_id": run_id,
        "mode": "live" if args.allow_paid_api else "dry_run",
        "status": "preflight_failed",
        "selected_page": args.page,
        "request_limits": {
            "concurrency": 1, "max_requests": 1, "automatic_retries": False,
            "cross_process_launch_cooldown_s": PILOT_COOLDOWN_SECONDS,
        },
        "preflight": {"status": "pending", "checks": {}, "errors": []},
        "request_outcome": None,
        "actual_usage": {"input_tokens": None, "output_tokens": None, "reasoning_tokens": None, "total_tokens": None},
        "estimated_cost_rub": None,
        "estimated_cost_method": "Reported token usage and operator tariff; unknown cached input is priced at the uncached input rate.",
        "reserved_possible_cost_rub": None,
        "structure": None,
        "duration_ms": None,
        "retention": {
            "retention_policy_days": PILOT_RETENTION_DAYS,
            "automatic_deletion": False,
            "delete_by_utc": (datetime.now(timezone.utc) + timedelta(days=PILOT_RETENTION_DAYS)).isoformat(),
            "transcription_file": "transcription.json",
            "deletion_method": "Delete the complete run directory after review and no later than the stated date.",
        },
        "errors": [],
    }
    transcription_path: Path | None = None
    try:
        if type(args.page) is not int or args.page < 1:
            raise ControlledPilotError("invalid_page_selection")
        if type(args.dpi) is not int or not 36 <= args.dpi <= MAX_PILOT_DPI:
            raise ControlledPilotError("dpi_limit")
        if type(args.requests_per_minute) is not int or args.requests_per_minute < 1:
            raise ControlledPilotError("rpm_invalid")
        if type(args.tokens_per_minute) is not int or args.tokens_per_minute < 1:
            raise ControlledPilotError("tpm_invalid")
        if type(args.input_token_upper_bound) is not int or args.input_token_upper_bound < 1:
            raise ControlledPilotError("input_token_bound_invalid")
        for name in ("input_rub_per_1k", "cached_input_rub_per_1k", "output_rub_per_1k", "budget_rub"):
            value = getattr(args, name, None)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ControlledPilotError("tariff_or_budget_invalid")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,96}", args.pricing_profile_id):
            raise ControlledPilotError("pricing_profile_invalid")
        if args.max_output_tokens < 1 or args.max_output_tokens > MAX_OUTPUT_TOKENS_CAP:
            raise ControlledPilotError("output_token_limit_invalid")
        if not math.isfinite(args.request_timeout_s) or args.request_timeout_s <= 0 or args.request_timeout_s > 300:
            raise ControlledPilotError("request_timeout_invalid")
        if args.allow_paid_api and not args.confirm_live_preflight:
            raise ControlledPilotError("live_preflight_confirmation_required")

        limits = VisionLimits(
            max_requests=1,
            max_pages=1,
            max_output_tokens=args.max_output_tokens,
            request_timeout_s=args.request_timeout_s,
            total_timeout_s=600.0,
            reasoning_effort="none" if args.disable_reasoning else None,
            dpi=args.dpi,
        )
        service_factory = settings_service_factory or AppSettingsService
        store_factory = secret_store_factory or create_secret_store
        try:
            settings = service_factory(args.data_dir).settings
            secret_store = store_factory(args.data_dir)
            adapter = QwenVisionAdapter.from_app_settings(
                settings.yandex,
                secret_store,
                limits=limits,
                quota_registry=quota_registry or _PILOT_QUOTA_REGISTRY,
                requests_per_minute=args.requests_per_minute,
                tokens_per_minute=args.tokens_per_minute,
                input_token_upper_bound=args.input_token_upper_bound,
                pricing_profile_id=args.pricing_profile_id,
                transport=transport,
            )
        except ControlledPilotError:
            raise
        except Exception:
            raise ControlledPilotError("ai_configuration_unavailable") from None

        model_ok, endpoint_ok = _allowed_model_and_endpoint(adapter.client.config)
        credential_ok = adapter.client.config.configured
        secure_store = getattr(secret_store, "is_insecure", True) is False
        environment_credential = bool(os.environ.get("AVERON_YANDEX_AI_API_KEY", "").strip())
        credential_source = (
            "environment" if environment_credential
            else "secure_store" if credential_ok and secure_store
            else "missing_or_insecure"
        )
        pricing = TokenPricing(
            profile_id=args.pricing_profile_id,
            input_rub_per_1k=args.input_rub_per_1k,
            cached_input_rub_per_1k=args.cached_input_rub_per_1k,
            output_rub_per_1k=args.output_rub_per_1k,
        )
        reserved_tokens = args.input_token_upper_bound + args.max_output_tokens
        maximum_cost = pricing.reserve_cost(args.input_token_upper_bound, args.max_output_tokens)
        checks = report["preflight"]["checks"]
        checks.update({
            "allowed_model": model_ok,
            "allowed_endpoint": endpoint_ok,
            "credentials_configured": credential_ok,
            "credential_store_secure": secure_store,
            "credential_source": credential_source,
            "rpm_configured": args.requests_per_minute,
            "tpm_configured": args.tokens_per_minute,
            "pricing_configured": True,
            "input_token_upper_bound_configured": args.input_token_upper_bound,
            "token_reservation": reserved_tokens,
            "max_output_tokens": args.max_output_tokens,
            "budget_rub": str(args.budget_rub),
            "maximum_reserved_cost_rub": _safe_cost(maximum_cost),
            "request_body_bytes": None,
            "image_width": None,
            "image_height": None,
            "image_bytes": None,
        })
        report["pricing"] = {
            "profile_id": args.pricing_profile_id,
            "currency": "RUB",
            "input_rub_per_1k": str(args.input_rub_per_1k),
            "cached_input_rub_per_1k": str(args.cached_input_rub_per_1k),
            "output_rub_per_1k": str(args.output_rub_per_1k),
        }
        report["input_token_upper_bound"] = {
            "value": args.input_token_upper_bound,
            "source": "operator_supplied; not independently verifiable by this runner",
        }
        if not model_ok:
            raise ControlledPilotError("unexpected_model_configuration")
        if not endpoint_ok:
            raise ControlledPilotError("unexpected_ai_endpoint")
        if not credential_ok:
            raise ControlledPilotError("missing_live_credentials_or_model")
        if environment_credential:
            raise ControlledPilotError("environment_credential_not_allowed")
        if not secure_store:
            raise ControlledPilotError("credential_store_insecure")
        if reserved_tokens > args.tokens_per_minute:
            raise ControlledPilotError("token_reservation_exceeds_tpm")
        if maximum_cost > args.budget_rub:
            raise ControlledPilotError("budget_below_reserved_maximum_cost")

        orchestration_config = OrchestrationConfig(
            concurrency=1,
            dpi=args.dpi,
            max_requests=1,
            requests_per_minute=args.requests_per_minute,
            tokens_per_minute=args.tokens_per_minute,
            max_budget_rub=args.budget_rub,
            pricing=pricing,
            max_image_bytes=limits.max_image_bytes,
            max_image_pixels=limits.max_image_pixels,
            max_pages=1000,
        )
        identity = inspect_pdf(args.pdf.expanduser(), adapter, orchestration_config, run_id=run_id)
        if args.page > identity.page_count:
            raise ControlledPilotError("invalid_page_selection")
        image = FitZPageRenderer().render(
            args.pdf.expanduser(), args.page, args.dpi, orchestration_config,
        )
        estimate = adapter.estimate(image)
        if estimate.reserved_input_tokens != args.input_token_upper_bound:
            raise ControlledPilotError("input_token_reservation_mismatch")
        if estimate.reserved_input_tokens + estimate.reserved_output_tokens > args.tokens_per_minute:
            raise ControlledPilotError("token_reservation_exceeds_tpm")
        checks.update({
            "pdf_page_count": identity.page_count,
            "source_sha256": identity.source_sha256,
            "request_body_bytes": adapter.client.measure_request_bytes(image.data, args.page),
            "image_width": image.width,
            "image_height": image.height,
            "image_bytes": len(image.data),
            "image_and_request_limits_passed": True,
            "budget_covers_worst_case_reservation": True,
        })
        report["preflight"]["status"] = "passed"
        report["preflight"]["errors"] = []
        report["configuration"] = {
            "model_family": "qwen3.6-35b-a3b",
            "model_uri_redacted": True,
            "endpoint_redacted": True,
            "credential_value_redacted": True,
            "credential_status": "configured_in_secure_store",
            "reasoning_effort": limits.reasoning_effort,
            "request_timeout_s": limits.request_timeout_s,
            "concurrency": 1,
            "max_requests": 1,
            "automatic_retries": False,
        }

        if not args.allow_paid_api:
            report["status"] = "dry_run_ready"
            report["preflight"]["operator_permission"] = "not_granted; no HTTP request was sent"
        else:
            if getattr(secret_store, "is_insecure", True):
                raise ControlledPilotError("credential_store_insecure")
            guard_factory = live_guard_factory or _LiveInvocationGuard
            # A custom --data-dir must not reset the one-request cooldown.
            with guard_factory(_pilot_guard_state_dir()) as guard:
                guard.mark_live_start()
                checkpoint = CheckpointStore(run_dir / "checkpoint", identity)
                orchestrator = PageOrchestrator(
                    adapter, FitZPageRenderer(), orchestration_config, checkpoint,
                )
                result = asyncio.run(_execute_one_page(
                    orchestrator, args.pdf.expanduser(), args.page, cancel_event,
                ))
                selected = _report_from_orchestration(result, args.page)
                private_extraction = selected.pop("_private_extraction")
                report.update(selected)
                report["expected_page_count"] = result.expected_page_count
                report["elapsed_ms"] = round(result.elapsed_s * 1000, 3)
                report["reserved_possible_cost_rub"] = _safe_cost(result.reserved_unknown_cost_rub)
                report["source_integrity_error_code"] = result.source_integrity_error_code
                if private_extraction is not None:
                    transcription_path = run_dir / "transcription.json"
                    _write_json_private(transcription_path, {
                        "format": "averon-qwen-vision-controlled-transcription-v1",
                        "source_sha256": result.source_sha256,
                        "page": args.page,
                        "extraction": private_extraction.model_dump(mode="json"),
                    })
                report["transcription_saved"] = transcription_path is not None
                report["status"] = selected["status"]
                if selected["request_outcome"]["safe_error_code"]:
                    report["errors"].append(selected["request_outcome"]["safe_error_code"])
    except ControlledPilotError as exc:
        preflight_incomplete = report["preflight"]["status"] != "passed"
        report["status"] = "preflight_failed" if preflight_incomplete else "completed_with_errors"
        report["errors"].append(exc.code)
        if preflight_incomplete:
            report["preflight"]["status"] = "failed"
            report["preflight"]["errors"].append(exc.code)
    except OrchestrationError as exc:
        preflight_incomplete = report["preflight"]["status"] != "passed"
        report["status"] = "preflight_failed" if preflight_incomplete else "completed_with_errors"
        report["errors"].append(exc.code)
        if preflight_incomplete:
            report["preflight"]["status"] = "failed"
            report["preflight"]["errors"].append(exc.code)
    except KeyboardInterrupt:
        report["status"] = "cancelled"
        report["errors"].append("cancelled_by_operator")
    except Exception:
        preflight_incomplete = report["preflight"]["status"] != "passed"
        report["status"] = "preflight_failed" if preflight_incomplete else "completed_with_errors"
        report["errors"].append("pilot_run_failed")
        if preflight_incomplete:
            report["preflight"]["status"] = "failed"
            report["preflight"]["errors"].append("pilot_run_failed")
    finally:
        report["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        try:
            _write_json_private(run_dir / "report.json", report)
        except Exception:
            raise ControlledPilotError("report_write_failed") from None

    exit_code = 0 if report["status"] in {"dry_run_ready", "completed"} else 1
    return exit_code, run_dir, report


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cancel_event = asyncio.Event()
    previous_sigint = signal.getsignal(signal.SIGINT)

    def request_cancel(_signum, _frame) -> None:
        cancel_event.set()

    signal.signal(signal.SIGINT, request_cancel)
    try:
        try:
            exit_code, run_dir, report = run(args, cancel_event=cancel_event)
        except ControlledPilotError as exc:
            print(json.dumps({"status": "failed", "error_code": exc.code}, ensure_ascii=False))
            return 2
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
    print(json.dumps({
        "status": report["status"],
        "run_id": report["run_id"],
        "report_file": str(run_dir / "report.json"),
        "transcription_saved": bool(report.get("transcription_saved", False)),
    }, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
