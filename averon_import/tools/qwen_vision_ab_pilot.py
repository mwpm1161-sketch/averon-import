"""Private B-then-A experiment; prepare/preflight never send HTTP requests.

Not an application entry point. Each paid case needs a separate explicit command.
The permanent intent files consume the two attempt slots before transport entry;
an interrupted/uncertain case can never be retried by this runner.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import json
import os
from pathlib import Path
import signal
import threading
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from averon_import.services.ai_first.orchestration import (
    FitZPageRenderer, OrchestrationConfig, TokenPricing, _sha256_file,
)
from averon_import.services.ai_first.private_acl import (
    verify_private_path, verify_private_tree,
)
from averon_import.services.ai_first.qwen_vision_adapter import QwenVisionAdapter
from averon_import.services.ai_request_diagnostics import AIRequestDiagnosticRecord, AIRequestDiagnostics
from averon_import.services.ocr.qwen_vision_client import (
    QwenVisionClient, QwenVisionConfig, QwenVisionError, VisionLimits, _json,
    validate_user_context,
)
from averon_import.services.secrets import YANDEX_AI_API_KEY
from averon_import.tools.qwen_vision_ab_payloads import ContextABPayload, prepare_context_ab
from averon_import.tools.qwen_vision_controlled_pilot import _allowed_model_and_endpoint, _LiveInvocationGuard
from averon_import.tools.qwen_vision_pilot import _secure_run_directory, _write_json_private


LIMITS = VisionLimits(max_output_tokens=32768, reasoning_effort="none", max_requests=1,
                      max_pages=1, dpi=115, request_timeout_s=300, total_timeout_s=600)
PRICING = TokenPricing(profile_id="yandex-qwen36-public-sync-20261028",
                       input_rub_per_1k=Decimal("0.20"), cached_input_rub_per_1k=Decimal("0.05"),
                       output_rub_per_1k=Decimal("0.30"))


class ABError(RuntimeError):
    pass


class Deployment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    model: str = Field(repr=False)
    base_url: str
    folder_id: str = Field(default="", repr=False)

    def config(self, api_key: str = "") -> QwenVisionConfig:
        config = QwenVisionConfig(self.base_url, self.model, api_key, self.folder_id, limits=LIMITS)
        if _allowed_model_and_endpoint(config) != (True, True):
            raise ABError("ab_deployment_not_allowed")
        folder = self.model.split("/")[2]
        if self.folder_id and self.folder_id != folder:
            raise ABError("ab_folder_mismatch")
        return replace(config, folder_id=folder)


class ABPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    format_version: Literal[1]
    created_utc: str
    expires_utc: str
    source_path: str = Field(repr=False)
    source_sha256: str
    image_sha256: str
    context_sha256: str = Field(repr=False)
    deployment: Deployment = Field(repr=False)
    baseline_input_bound: int = Field(gt=0)
    budget_rub: Decimal = Field(gt=0)
    local_rpm_limit: int = Field(gt=0)
    local_tpm_limit: int = Field(gt=0)
    body_sha256: dict[str, str] = Field(repr=False)
    native_body_bytes: dict[str, int]
    request_body_bytes: int = Field(gt=0)
    image_width: int = Field(gt=0)
    image_height: int = Field(gt=0)
    limits_sha256: str
    pricing_sha256: str


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _settings_hash(value: Any) -> str:
    return _sha(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode())


def _read_private(path: Path, maximum: int) -> bytes:
    verify_private_path(path, directory=False)
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ABError("ab_private_file_too_large")
    return raw


def _hint(path: Path) -> str:
    text = _read_private(path, 4000).decode("utf-8")
    if validate_user_context(text) is None:
        raise ABError("ab_context_required")
    return text


def _write_bytes(path: Path, raw: bytes) -> None:
    # New child of a verified private directory; inspect DACL before writing.
    with path.open("xb") as stream:
        if os.name != "nt":
            os.chmod(path, 0o600)
        verify_private_path(path, directory=False)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    verify_private_path(path, directory=False)


def _outside_repository(root: Path) -> None:
    # Resolve symlinks as well as normal parents; never store client artifacts in Git.
    root = root.resolve()
    if any((parent / ".git").exists() for parent in (root, *root.parents)):
        raise ABError("ab_output_inside_repository")


def _payloads(config: QwenVisionConfig, image: bytes, hint: str) -> dict[str, ContextABPayload]:
    a, b, _ = prepare_context_ab(QwenVisionClient(config), image, 1, hint)
    return {"B": b, "A": a}  # C is deliberately unavailable to this experiment.


def _report(plan: ABPlan, payloads: dict[str, ContextABPayload], image: bytes, hint: str) -> dict[str, Any]:
    client = QwenVisionClient(plan.deployment.config())
    delta = payloads["A"].request_bytes - client.measure_request_bytes(image, 1)
    bound = plan.baseline_input_bound + delta
    if bound + LIMITS.max_output_tokens > 262144:
        raise ABError("ab_context_reservation_exceeded")
    if bound + LIMITS.max_output_tokens > plan.local_tpm_limit:
        raise ABError("ab_local_tpm_exceeded")
    reservation = PRICING.reserve_cost(bound, LIMITS.max_output_tokens)
    if reservation * 2 > plan.budget_rub:
        raise ABError("ab_pair_budget_exceeded")
    body = json.loads(payloads["B"].body)
    return {
        "status": "dry_run_ready", "http_attempts": 0, "case_order": ["B", "A"],
        "page": 1, "dpi": 115, "crop": None, "image_sha256": plan.image_sha256,
        "image_width": plan.image_width, "image_height": plan.image_height,
        "image_bytes": len(image), "request_body_bytes": plan.request_body_bytes,
        "native_body_bytes": plan.native_body_bytes,
        "system_message_counts": {"B": 1, "A": 2},
        "context_chars": len(hint), "context_utf8_bytes": len(hint.encode("utf-8")),
        "model": "qwen3.6-35b-a3b", "endpoint": client.config.base_url + "/chat/completions",
        "schema_sha256": _sha(json.dumps(body["response_format"]["json_schema"]["schema"],
                                        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()),
        "response_format_sha256": _settings_hash(body["response_format"]),
        "max_completion_tokens": 32768, "reasoning_effort": "none", "temperature": 0,
        "stream": False, "store": False, "max_http_attempts": 2, "concurrency": 1,
        "automatic_retries": 0, "cooldown_seconds": 360,
        "local_rpm_limit": plan.local_rpm_limit, "local_tpm_limit": plan.local_tpm_limit,
        "account_quota_status": "unconfirmed", "account_rpm_verified": None, "account_tpm_verified": None,
        "external_credential_traffic_verified": False,
        "baseline_input_bound": plan.baseline_input_bound, "context_token_reservation": delta,
        "reserved_input_tokens": bound, "reserved_total_tokens": bound + 32768,
        "input_rub_per_1k": "0.20", "cached_input_rub_per_1k": "0.05", "output_rub_per_1k": "0.30",
        "reserved_rub_per_case": str(reservation), "reserved_rub_pair": str(reservation * 2),
        "max_budget_rub": str(plan.budget_rub), "acl_verified": True,
        "expires_utc": plan.expires_utc, "credentials_checked": False,
    }


def prepare(*, pdf: Path, context_file: Path, deployment_file: Path, output_root: Path,
            input_bound: int, budget: Decimal, rpm: int, tpm: int) -> tuple[Path, dict[str, Any]]:
    """Freeze once without credentials, app settings, checkpoints, or network access."""
    _outside_repository(output_root)
    hint = _hint(context_file)
    deployment = Deployment.model_validate(_json(_read_private(deployment_file, 16384)))
    config = deployment.config()
    before = _sha256_file(pdf)
    image = FitZPageRenderer().render(pdf, 1, 115, OrchestrationConfig(
        dpi=115, max_budget_rub=budget, pricing=PRICING,
        max_image_pixels=LIMITS.max_image_pixels, max_image_bytes=LIMITS.max_image_bytes,
    ))
    if before != _sha256_file(pdf):
        raise ABError("ab_source_changed")
    payloads = _payloads(config, image.data, hint)
    now = datetime.now(timezone.utc)
    plan = ABPlan(
        format_version=1, created_utc=now.isoformat(), expires_utc=(now + timedelta(days=7)).isoformat(),
        source_path=str(pdf.resolve()), source_sha256=before, image_sha256=_sha(image.data),
        context_sha256=_sha(hint.encode("utf-8")), deployment=deployment,
        baseline_input_bound=input_bound, budget_rub=budget, local_rpm_limit=rpm, local_tpm_limit=tpm,
        body_sha256={key: _sha(value.body) for key, value in payloads.items()},
        native_body_bytes={"A": payloads["A"].request_bytes, "B": payloads["B"].request_bytes - 31},
        request_body_bytes=payloads["A"].request_bytes, image_width=image.width, image_height=image.height,
        limits_sha256=_settings_hash(asdict(LIMITS)),
        pricing_sha256=_settings_hash(PRICING.model_dump(mode="json")),
    )
    report = _report(plan, payloads, image.data, hint)
    run = _secure_run_directory(output_root)
    _write_bytes(run / "image.png", image.data)
    _write_bytes(run / "context.txt", hint.encode("utf-8"))
    _write_json_private(run / "plan.json", plan.model_dump(mode="json"))
    _write_json_private(run / "preflight.json", report)
    verify_private_tree(run)
    return run, report


def preflight(run: Path) -> tuple[ABPlan, dict[str, ContextABPayload], bytes, str, dict[str, Any]]:
    _outside_repository(run)
    verify_private_tree(run)
    plan = ABPlan.model_validate_json(_read_private(run / "plan.json", 32768))
    if datetime.now(timezone.utc) >= datetime.fromisoformat(plan.expires_utc):
        raise ABError("ab_bundle_expired")
    if (plan.limits_sha256 != _settings_hash(asdict(LIMITS))
            or plan.pricing_sha256 != _settings_hash(PRICING.model_dump(mode="json"))):
        raise ABError("ab_configuration_changed")
    image, hint = _read_private(run / "image.png", LIMITS.max_image_bytes), _hint(run / "context.txt")
    if (_sha(image) != plan.image_sha256 or _sha(hint.encode("utf-8")) != plan.context_sha256
            or _sha256_file(Path(plan.source_path)) != plan.source_sha256):
        raise ABError("ab_artifact_changed")
    payloads = _payloads(plan.deployment.config(), image, hint)
    if ({key: _sha(value.body) for key, value in payloads.items()} != plan.body_sha256
            or any(value.request_bytes != plan.request_body_bytes for value in payloads.values())):
        raise ABError("ab_payload_changed")
    report = _report(plan, payloads, image, hint)
    report["attempt_slots_consumed"] = sum((run / f"{case}-intent.json").exists() for case in ("B", "A"))
    return plan, payloads, image, hint, report


class _PreparedClient(QwenVisionClient):
    def __init__(self, config, payload, image, hint, *, transport):
        super().__init__(config, transport=transport)
        self.payload, self.image, self.hint = payload, image, hint

    def _request_body(self, image, page, user_context=None):
        if user_context is None:
            return super()._request_body(image, page)
        if image != self.image or page != 1 or user_context != self.hint:
            raise ABError("ab_payload_changed")
        return self.payload.body


def dispatch(run: Path, case: Literal["B", "A"], *, allow_paid_api: bool,
             confirm_live_preflight: bool, accept_unverified_account_quotas: bool,
             confirm_exclusive_key_use: bool, api_key: str, cancel: threading.Event | None = None,
             transport=None) -> dict[str, Any]:
    """One case per invocation; injectable fake transport only in offline tests.

    No code calls this function from prepare/preflight. Nothing schedules A.
    A intent without a terminal result is permanently uncertain, never retryable.
    """
    if not all((allow_paid_api, confirm_live_preflight, accept_unverified_account_quotas,
                confirm_exclusive_key_use)):
        raise ABError("ab_explicit_permission_required")
    if case not in ("B", "A") or not api_key.strip():
        raise ABError("ab_case_or_credentials_invalid")
    preflight(run)  # Validate the bundle before creating even its isolated lock.
    # This guard is private to this bundle, and does not claim to lock other clients.
    # A separate bundle is not another permission: operator must use only this bundle.
    with _LiveInvocationGuard(run) as guard:
        plan, payloads, image, hint, _ = preflight(run)
        credential_binding = hmac.new(api_key.encode("utf-8"),
                                      b"averon-private-ab-credential-v1\0" + _read_private(run / "plan.json", 32768),
                                      hashlib.sha256).hexdigest()
        if (run / f"{case}-intent.json").exists():
            raise ABError("ab_attempt_already_consumed")
        if case == "A":
            if not (run / "B-intent.json").is_file():
                raise ABError("ab_case_order_invalid")
            if not (run / "B-report.json").is_file():
                raise ABError("ab_previous_outcome_unknown")
            previous = _json(_read_private(run / "B-report.json", 32768))
            previous_intent = _json(_read_private(run / "B-intent.json", 32768))
            if not hmac.compare_digest(previous_intent.get("credential_binding", ""), credential_binding):
                raise ABError("ab_credentials_changed")
            if (previous.get("outcome") not in ("completed", "rejected")
                    or previous.get("case") != "B" or previous.get("http_attempts") != 1):
                raise ABError("ab_previous_outcome_blocks_next_case")
        elif (run / "A-intent.json").exists():
            raise ABError("ab_case_order_invalid")
        if cancel is not None and cancel.is_set():
            raise ABError("ab_cancelled_before_dispatch")
        # Construct client before consuming a slot; it sends only on extract().
        client = _PreparedClient(plan.deployment.config(api_key), payloads[case], image, hint, transport=transport)
        started = datetime.now(timezone.utc).isoformat()
        _write_json_private(run / f"{case}-intent.json", {
            "format_version": 1, "case": case, "started_utc": started,
            "reserved_rub": _report(plan, payloads, image, hint)["reserved_rub_per_case"],
            "credential_binding": credential_binding,  # private only; never in reports
            "outcome": "unknown_outcome",  # never auto-release the reservation
        })
        with (run / f"{case}-intent.json").open("r+b") as intent:
            os.fsync(intent.fileno())
        if os.name != "nt":
            directory_fd = os.open(run, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        guard.mark_live_start()
        verify_private_tree(run)
        completion = None
        try:
            completion = client.extract(image, 1, user_context=hint, cancel=cancel)
            diagnostics, code = completion.diagnostics, None
        except QwenVisionError as exc:
            diagnostics, code = exc.diagnostics, exc.code
        usage = QwenVisionAdapter._usage(diagnostics.get("usage"))
        cost = PRICING.actual_cost(usage) if usage is not None else None
        status = diagnostics.get("http_status")
        outcome = "completed" if completion else (
            "rejected" if status == 400 and code == "http_error" else "unknown_outcome")
        input_reservation = _report(plan, payloads, image, hint)["reserved_input_tokens"]
        if usage and ((usage.input_tokens is not None and usage.input_tokens > input_reservation)
                      or (usage.output_tokens is not None and usage.output_tokens > LIMITS.max_output_tokens)):
            outcome = "unknown_outcome"  # No second case after a broken reservation assumption.
        evidence = AIRequestDiagnostics.model_validate(diagnostics.get("request_diagnostics", {}))
        evidence.system_message_count = payloads[case].system_message_count
        record = AIRequestDiagnosticRecord(
            format_version=1, checkpoint_identity_fingerprint=_sha(_read_private(run / "plan.json", 32768)),
            page_number=1, attempt_started_at=started, http_status=status,
            finish_reason=diagnostics.get("finish_reason"), request_latency_ms=diagnostics.get("latency_ms"),
            evidence=evidence,
        )
        report = {
            "format_version": 1, "case": case, "outcome": outcome, "error_code": code,
            "http_attempts": int(bool(diagnostics.get("request_attempted"))), "http_status": status,
            "finish_reason": diagnostics.get("finish_reason"), "latency_ms": diagnostics.get("latency_ms"),
            "usage": usage.model_dump(mode="json") if usage else None,
            "estimated_cost_rub": str(cost) if cost is not None else None,
            "account_quota_status": "unconfirmed", "unverified_quota_risk_accepted": True,
            "external_credential_traffic_verified": False,
        }
        # Sidecar/report creation failure leaves the permanent intent uncertain.
        _write_json_private(run / f"{case}-diagnostics-v1.json", record.model_dump(mode="json"))
        if completion is not None:
            _write_json_private(run / f"{case}-transcription.json", completion.page.model_dump(mode="json"))
        _write_json_private(run / f"{case}-report.json", report)
        verify_private_tree(run)
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    for name in ("pdf", "context-file", "deployment-file", "output-root"):
        prep.add_argument("--" + name, type=Path, required=True)
    prep.add_argument("--input-token-upper-bound", type=int, required=True)
    prep.add_argument("--budget-rub", type=Decimal, required=True)
    prep.add_argument("--rpm-limit", type=int, required=True)
    prep.add_argument("--tpm-limit", type=int, required=True)
    check = commands.add_parser("preflight")
    check.add_argument("--run-dir", type=Path, required=True)
    live = commands.add_parser("run-case")
    live.add_argument("--run-dir", type=Path, required=True)
    live.add_argument("--case", choices=("B", "A"), required=True)
    for name in ("allow-paid-api", "confirm-live-preflight", "accept-unverified-account-quotas",
                 "confirm-exclusive-key-use", "use-windows-credential-manager"):
        live.add_argument("--" + name, action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            run, report = prepare(pdf=args.pdf, context_file=args.context_file,
                                  deployment_file=args.deployment_file, output_root=args.output_root,
                                  input_bound=args.input_token_upper_bound, budget=args.budget_rub,
                                  rpm=args.rpm_limit, tpm=args.tpm_limit)
            print(json.dumps({"run_dir": str(run), "preflight": report}, ensure_ascii=False))
        elif args.command == "preflight":
            print(json.dumps(preflight(args.run_dir)[-1], ensure_ascii=False))
        else:
            # Resolve a key only with all explicit permissions; never writes the key store.
            flags = (args.allow_paid_api, args.confirm_live_preflight,
                     args.accept_unverified_account_quotas, args.confirm_exclusive_key_use)
            if not all(flags):
                raise ABError("ab_explicit_permission_required")
            key = os.environ.get("AVERON_YANDEX_AI_API_KEY", "")
            if args.use_windows_credential_manager:
                if os.name != "nt" or key:
                    raise ABError("ab_credential_source_ambiguous")
                from averon_import.services.secrets import WindowsCredentialStore
                key = WindowsCredentialStore().get(YANDEX_AI_API_KEY) or ""
            cancel = threading.Event()
            previous = signal.signal(signal.SIGINT, lambda *_: cancel.set())
            try:
                report = dispatch(args.run_dir, args.case, allow_paid_api=True, confirm_live_preflight=True,
                                  accept_unverified_account_quotas=True, confirm_exclusive_key_use=True,
                                  api_key=key, cancel=cancel)
            finally:
                signal.signal(signal.SIGINT, previous)
            print(json.dumps(report, ensure_ascii=False))
            return 0 if report["outcome"] == "completed" else 1
        return 0
    except Exception as exc:
        # No traceback/provider text/path/context in console diagnostics.
        print(json.dumps({"status": "blocked", "code": str(exc) if isinstance(exc, ABError) else "ab_preflight_or_storage_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
