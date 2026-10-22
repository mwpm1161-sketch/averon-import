from __future__ import annotations

import asyncio
from decimal import Decimal
import json
import os
from pathlib import Path
from types import SimpleNamespace
import time
import uuid

import fitz
import pytest

from averon_import.services.ocr.qwen_vision_client import _HttpsTransport, VisionHttpResponse
from averon_import.tools import qwen_vision_controlled_pilot as pilot


SYNTHETIC_KEY = "controlled-pilot-synthetic-key"
SYNTHETIC_CELL = "synthetic test description"


@pytest.fixture(autouse=True)
def prohibit_real_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("controlled pilot tests must never use live HTTP")

    monkeypatch.setattr(_HttpsTransport, "post", denied)

    def private_synthetic_run_dir(root: Path) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        run_dir = root / f"synthetic-run-{uuid.uuid4().hex}"
        run_dir.mkdir(mode=0o700)
        if os.name != "nt":
            os.chmod(run_dir, 0o700)
        return run_dir

    # Existing V1.2 tests exercise the real ACL helper. Use temporary synthetic
    # output directories here so this integration suite stays fast on Windows.
    monkeypatch.setattr(pilot, "_secure_run_directory", private_synthetic_run_dir)
    monkeypatch.setattr(pilot.CheckpointStore, "_secure_tree", lambda _self: None)


@pytest.fixture
def synthetic_pdf(tmp_path):
    path = tmp_path / "synthetic-controlled-pilot.pdf"
    with fitz.open() as document:
        for index in range(3):
            page = document.new_page(width=72, height=72)
            page.insert_text((4, 20), f"synthetic page {index + 1}")
        document.save(path)
    return path


class SecureTestSecretStore:
    is_insecure = False

    def __init__(self, *, key: str | None = SYNTHETIC_KEY):
        self.key = key

    def get(self, _name: str) -> str | None:
        return self.key


class FakeTransport:
    def __init__(self, response=None, *, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        request = {
            "url": url,
            "payload": json.loads(kwargs["body"].decode("utf-8")),
        }
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return self.response


class NoopLiveGuard:
    def __init__(self, _state_dir):
        self.started = False

    def __enter__(self):
        return self

    def mark_live_start(self):
        self.started = True

    def __exit__(self, *_args):
        return None


def _settings_factory(*, model="gpt://test-folder/qwen3.6-35b-a3b/latest",
                      endpoint="https://ai.api.cloud.yandex.net/v1"):
    settings = SimpleNamespace(yandex=SimpleNamespace(
        folder_id="test-folder", llm_model=model, llm_base_url=endpoint,
    ))
    return lambda _data_dir: SimpleNamespace(settings=settings)


def _args(tmp_path: Path, pdf: Path, *extra: str):
    return pilot._parser().parse_args([
        "--pdf", str(pdf), "--page", "2", "--dpi", "72",
        "--requests-per-minute", "1", "--tokens-per-minute", "50000",
        "--input-token-upper-bound", "10000",
        "--input-rub-per-1k", "0.20", "--cached-input-rub-per-1k", "0.05",
        "--output-rub-per-1k", "0.30", "--pricing-profile-id", "synthetic-tariff-v1",
        "--budget-rub", "20", "--data-dir", str(tmp_path / "settings"),
        "--output-dir", str(tmp_path / "pilot-output"), *extra,
    ])


def _vision_payload(page: int = 2) -> dict:
    return {
        "page": page,
        "tables": [{
            "headers": ["Description"], "continues_previous_page": False,
            "bbox_candidate": None,
            "rows": [{
                "kind": "item", "logical_row_index": None, "continuation_of": None,
                "continues_previous_page": False, "struck_through": False,
                "handwritten": False, "handwritten_replacements": [],
                "bbox_candidate": None,
                "cells": [{
                    "column_index": 0, "column_span": 1, "row_span": 1,
                    "raw_text": SYNTHETIC_CELL, "alternatives": [],
                    "handwritten_replacements": [], "unreadable": False,
                    "struck_through": False, "handwritten": False,
                    "bbox_candidate": None,
                }],
            }],
        }],
        "unreadable_fragments": [], "handwritten_replacements": [],
    }


def _response(page: int = 2, *, status: int = 200, finish_reason: str = "stop") -> VisionHttpResponse:
    body = {
        "choices": [{
            "finish_reason": finish_reason,
            "message": {"content": json.dumps(_vision_payload(page), ensure_ascii=False)},
        }],
        "usage": {
            "prompt_tokens": 120, "completion_tokens": 70, "total_tokens": 190,
            "completion_tokens_details": {"reasoning_tokens": 12},
        },
    }
    if status >= 400:
        body = {"usage": body["usage"]}
    return VisionHttpResponse(status, json.dumps(body).encode("utf-8"), {})


def _run(tmp_path, pdf, *, args=None, transport=None, settings_factory=None,
         store_factory=None, cancel_event=None):
    return pilot.run(
        args or _args(tmp_path, pdf),
        settings_service_factory=settings_factory or _settings_factory(),
        secret_store_factory=store_factory or (lambda _path: SecureTestSecretStore()),
        transport=transport or FakeTransport(_response()),
        quota_registry=pilot.QwenVisionQuotaRegistry(),
        cancel_event=cancel_event,
        live_guard_factory=NoopLiveGuard,
    )


def test_default_is_offline_preflight_and_does_not_save_transcription(tmp_path, synthetic_pdf):
    transport = FakeTransport(_response())
    code, run_dir, report = _run(tmp_path, synthetic_pdf, transport=transport)

    assert code == 0
    assert report["status"] == "dry_run_ready"
    assert report["preflight"]["status"] == "passed"
    assert report["selected_page"] == 2
    assert report["request_limits"] == {
        "concurrency": 1, "max_requests": 1, "automatic_retries": False,
        "cross_process_launch_cooldown_s": 360,
    }
    assert report["preflight"]["checks"]["request_body_bytes"] > 0
    assert report["preflight"]["checks"]["request_body_bytes"] < 6 * 1024 * 1024
    assert report["preflight"]["checks"]["image_and_request_limits_passed"] is True
    assert report["preflight"]["checks"]["token_reservation"] == 42768
    assert report["preflight"]["checks"]["budget_covers_worst_case_reservation"] is True
    assert report["retention"]["automatic_deletion"] is False
    assert transport.calls == []
    assert not (run_dir / "transcription.json").exists()


def test_invalid_pdf_is_a_preflight_failure_and_never_reaches_transport(tmp_path, synthetic_pdf):
    invalid_pdf = tmp_path / "not-a-pdf.pdf"
    invalid_pdf.write_text("synthetic invalid PDF", encoding="utf-8")
    transport = FakeTransport(_response())
    code, _run_dir, report = _run(tmp_path, invalid_pdf, transport=transport)
    assert code != 0
    assert report["status"] == "preflight_failed"
    assert report["preflight"]["status"] == "failed"
    assert "source_pdf_invalid" in report["preflight"]["errors"]
    assert transport.calls == []


def test_output_root_inside_repository_is_rejected_before_directory_creation(tmp_path, synthetic_pdf):
    args = _args(tmp_path, synthetic_pdf)
    forbidden_root = Path(pilot.__file__).resolve().parents[2] / ".controlled-pilot-test-must-not-exist"
    args.output_dir = forbidden_root
    with pytest.raises(pilot.ControlledPilotError, match="pilot_output_must_be_outside_repository"):
        pilot.run(args)
    assert not forbidden_root.exists()


@pytest.mark.parametrize("model,endpoint,error_code", [
    ("gpt://test-folder/other-model/latest", "https://ai.api.cloud.yandex.net/v1", "unexpected_model_configuration"),
    ("gpt://test-folder/qwen3.6-35b-a3b/latest", "https://example.invalid/v1", "unexpected_ai_endpoint"),
])
def test_preflight_rejects_unapproved_model_or_endpoint(tmp_path, synthetic_pdf, model, endpoint, error_code):
    transport = FakeTransport(_response())
    code, _run_dir, report = _run(
        tmp_path, synthetic_pdf, transport=transport,
        settings_factory=_settings_factory(model=model, endpoint=endpoint),
    )
    assert code != 0
    assert report["status"] == "preflight_failed"
    assert error_code in report["preflight"]["errors"]
    assert transport.calls == []


def test_preflight_rejects_missing_or_insecure_credentials(tmp_path, synthetic_pdf):
    args = _args(tmp_path, synthetic_pdf)
    code, _run_dir, report = _run(
        tmp_path, synthetic_pdf, args=args,
        store_factory=lambda _path: SecureTestSecretStore(key=None),
    )
    assert code != 0
    assert "missing_live_credentials_or_model" in report["errors"]

    class InsecureStore(SecureTestSecretStore):
        is_insecure = True

    code, _run_dir, report = _run(
        tmp_path, synthetic_pdf, args=args,
        store_factory=lambda _path: InsecureStore(),
    )
    assert code != 0
    assert "credential_store_insecure" in report["errors"]


def test_preflight_rejects_environment_key_override_without_disclosing_value(tmp_path, synthetic_pdf, monkeypatch):
    monkeypatch.setenv("AVERON_YANDEX_AI_API_KEY", SYNTHETIC_KEY)
    args = _args(tmp_path, synthetic_pdf, "--allow-paid-api", "--confirm-live-preflight")
    transport = FakeTransport(_response())
    code, run_dir, report = _run(tmp_path, synthetic_pdf, args=args, transport=transport)
    assert code != 0
    assert "environment_credential_not_allowed" in report["errors"]
    assert report["preflight"]["checks"]["credential_source"] == "environment"
    assert SYNTHETIC_KEY not in (run_dir / "report.json").read_text(encoding="utf-8")
    assert transport.calls == []


@pytest.mark.parametrize("change,error_code", [
    ({"budget_rub": Decimal("1")}, "budget_below_reserved_maximum_cost"),
    ({"tokens_per_minute": 1000}, "token_reservation_exceeds_tpm"),
    ({"output_rub_per_1k": Decimal("NaN")}, "tariff_or_budget_invalid"),
])
def test_preflight_rejects_budget_tpm_and_invalid_tariff(tmp_path, synthetic_pdf, change, error_code):
    args = _args(tmp_path, synthetic_pdf)
    for name, value in change.items():
        setattr(args, name, value)
    transport = FakeTransport(_response())
    code, _run_dir, report = _run(tmp_path, synthetic_pdf, args=args, transport=transport)
    assert code != 0
    assert error_code in report["errors"]
    assert transport.calls == []


def test_live_requires_both_explicit_permission_flags(tmp_path, synthetic_pdf):
    args = _args(tmp_path, synthetic_pdf, "--allow-paid-api")
    transport = FakeTransport(_response())
    code, _run_dir, report = _run(tmp_path, synthetic_pdf, args=args, transport=transport)
    assert code != 0
    assert "live_preflight_confirmation_required" in report["preflight"]["errors"]
    assert transport.calls == []


def test_live_sends_only_selected_page_once_and_saves_private_transcription(tmp_path, synthetic_pdf):
    args = _args(tmp_path, synthetic_pdf, "--allow-paid-api", "--confirm-live-preflight")
    transport = FakeTransport(_response())
    code, run_dir, report = _run(tmp_path, synthetic_pdf, args=args, transport=transport)

    assert code == 0
    assert report["status"] == "completed"
    assert report["selected_page"] == 2
    assert report["request_outcome"]["page_state"] == "completed"
    assert report["request_outcome"]["attempt_count"] == 1
    assert report["request_outcome"]["http_status"] == 200
    assert report["request_outcome"]["http_status_class"] == "2xx"
    assert report["request_outcome"]["finish_reason"] == "stop"
    assert report["request_outcome"]["request_latency_ms"] is not None
    assert report["request_outcome"]["page_elapsed_ms"] is not None
    assert report["actual_usage"] == {
        "input_tokens": 120, "output_tokens": 70,
        "reasoning_tokens": 12, "total_tokens": 190,
    }
    assert report["estimated_cost_rub"] is not None
    assert report["structure"]["source_row_count"] == 1
    assert report["structure"]["records_requiring_review_count"] == 1
    assert report["structure"]["completeness_verified"] is False
    assert report["transcription_saved"] is True
    assert len(transport.calls) == 1
    assert "Source page number: 2" in transport.calls[0]["payload"]["messages"][1]["content"][0]["text"]
    assert transport.calls[0]["payload"]["max_completion_tokens"] == args.max_output_tokens
    assert SYNTHETIC_CELL in (run_dir / "transcription.json").read_text(encoding="utf-8")
    safe_report = (run_dir / "report.json").read_text(encoding="utf-8")
    assert SYNTHETIC_KEY not in safe_report
    assert SYNTHETIC_CELL not in safe_report
    assert "base64" not in safe_report.casefold()


def test_cancel_before_dispatch_does_not_send_http(tmp_path, synthetic_pdf):
    args = _args(tmp_path, synthetic_pdf, "--allow-paid-api", "--confirm-live-preflight")
    event = asyncio.Event()
    event.set()
    transport = FakeTransport(_response())
    code, _run_dir, report = _run(
        tmp_path, synthetic_pdf, args=args, transport=transport, cancel_event=event,
    )
    assert code != 0
    assert report["status"] == "cancelled"
    assert report["actual_usage"]["input_tokens"] is None
    assert transport.calls == []


def test_cancel_after_dispatch_records_unknown_and_does_not_retry(tmp_path, synthetic_pdf):
    args = _args(tmp_path, synthetic_pdf, "--allow-paid-api", "--confirm-live-preflight")
    event = asyncio.Event()

    class CancelAfterDispatchTransport(FakeTransport):
        def post(self, url, **kwargs):
            request = {"url": url, "payload": json.loads(kwargs["body"].decode("utf-8"))}
            self.calls.append(request)
            loop = event._loop
            assert loop is not None
            loop.call_soon_threadsafe(event.set)
            time.sleep(0.02)
            raise TimeoutError("synthetic transport timeout after dispatch")

    transport = CancelAfterDispatchTransport()
    code, _run_dir, report = _run(
        tmp_path, synthetic_pdf, args=args, transport=transport, cancel_event=event,
    )
    assert code != 0
    assert report["status"] == "cancelled"
    assert report["request_outcome"]["page_state"] == "unknown_outcome"
    assert report["request_outcome"]["request_attempted"] is True
    assert report["actual_usage"]["input_tokens"] is None
    assert Decimal(report["reserved_possible_cost_rub"]) > 0
    assert len(transport.calls) == 1


def test_http_503_is_unknown_and_not_retried_but_keeps_usage(tmp_path, synthetic_pdf):
    args = _args(tmp_path, synthetic_pdf, "--allow-paid-api", "--confirm-live-preflight")
    transport = FakeTransport(_response(status=503))
    code, _run_dir, report = _run(tmp_path, synthetic_pdf, args=args, transport=transport)
    assert code != 0
    assert report["status"] == "unknown_outcome"
    assert report["request_outcome"]["http_status"] == 503
    assert report["actual_usage"]["input_tokens"] == 120
    assert report["actual_usage"]["output_tokens"] == 70
    assert report["estimated_cost_rub"] is not None
    assert len(transport.calls) == 1


def test_http_429_is_reported_as_failure_without_retry(tmp_path, synthetic_pdf):
    args = _args(tmp_path, synthetic_pdf, "--allow-paid-api", "--confirm-live-preflight")
    transport = FakeTransport(_response(status=429))
    code, _run_dir, report = _run(tmp_path, synthetic_pdf, args=args, transport=transport)
    assert code != 0
    assert report["status"] == "completed_with_errors"
    assert report["request_outcome"]["page_state"] == "failed"
    assert report["request_outcome"]["http_status"] == 429
    assert report["actual_usage"]["input_tokens"] == 120
    assert len(transport.calls) == 1


def test_incomplete_response_reports_finish_reason_and_is_not_retried(tmp_path, synthetic_pdf):
    args = _args(tmp_path, synthetic_pdf, "--allow-paid-api", "--confirm-live-preflight")
    transport = FakeTransport(_response(finish_reason="length"))
    code, _run_dir, report = _run(tmp_path, synthetic_pdf, args=args, transport=transport)
    assert code != 0
    assert report["status"] == "completed_with_errors"
    assert report["request_outcome"]["finish_reason"] == "length"
    assert report["request_outcome"]["http_status"] == 200
    assert report["actual_usage"]["input_tokens"] == 120
    assert report["structure"]["response_schema_accepted"] is False
    assert report["structure"]["completeness_verified"] is False
    assert len(transport.calls) == 1


def test_transport_error_is_unknown_keeps_reserved_possible_cost_and_no_retry(tmp_path, synthetic_pdf):
    args = _args(tmp_path, synthetic_pdf, "--allow-paid-api", "--confirm-live-preflight")
    transport = FakeTransport(error=OSError(f"private {SYNTHETIC_KEY} response text"))
    code, run_dir, report = _run(tmp_path, synthetic_pdf, args=args, transport=transport)
    assert code != 0
    assert report["status"] == "unknown_outcome"
    assert report["actual_usage"]["input_tokens"] is None
    assert report["estimated_cost_rub"] is None
    assert Decimal(report["reserved_possible_cost_rub"]) > 0
    assert len(transport.calls) == 1
    assert SYNTHETIC_KEY not in (run_dir / "report.json").read_text(encoding="utf-8")


def test_persistent_guard_enforces_cooldown_across_runner_restarts(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "PILOT_COOLDOWN_SECONDS", 3600)
    state_dir = tmp_path / "stable-user-state"
    with pilot._LiveInvocationGuard(state_dir) as guard:
        guard.mark_live_start()

    with pytest.raises(pilot.ControlledPilotError, match="pilot_cooldown_active"):
        with pilot._LiveInvocationGuard(state_dir):
            pass
