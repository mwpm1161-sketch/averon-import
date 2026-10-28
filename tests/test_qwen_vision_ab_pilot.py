"""Only synthetic PDFs/hints and fake HTTP. No credentials or live requests."""
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import socket
import threading

import fitz
import pytest

from averon_import.services.ai_first.private_acl import create_private_directory, verify_private_tree
from averon_import.services.ai_first.private_acl import PrivateAclError
from averon_import.services.ocr.qwen_vision_client import VisionHttpResponse, _HttpsTransport
from averon_import.tools import qwen_vision_ab_pilot as ab
from averon_import.tools.qwen_vision_controlled_pilot import ControlledPilotError


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def deny(*_, **__):
        pytest.fail("The experiment test must never use network")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(_HttpsTransport, "post", deny)


@pytest.fixture
def inputs(tmp_path):
    root = create_private_directory(tmp_path / "private")
    pdf = root / "synthetic.pdf"
    doc = fitz.open()
    for _ in range(2):
        page = doc.new_page(width=200, height=150)
        page.insert_text((30, 40), "Synthetic part")
    ab._write_bytes(pdf, doc.tobytes())
    doc.close()
    hint = root / "hint.txt"
    ab._write_bytes(hint, "Synthetic: preserve crossed-out originals.\n".encode())
    deployment = root / "deployment.json"
    ab._write_json_private(deployment, {
        "model": "gpt://synthetic-folder/qwen3.6-35b-a3b",
        "base_url": "https://ai.api.cloud.yandex.net/v1",
    })
    return dict(pdf=pdf, context_file=hint, deployment_file=deployment,
                output_root=root / "experiments", input_bound=16384,
                budget=Decimal("100"), rpm=2, tpm=60000)


@pytest.fixture
def bundle(inputs):
    return ab.prepare(**inputs)[0]


class Fake:
    def __init__(self, status=200, error=False):
        self.calls = []
        self.status, self.error = status, error

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise TimeoutError
        if self.status != 200:
            return VisionHttpResponse(self.status, json.dumps({"error": {
                "code": "unsupported_parameter", "param": "messages",
                "message": "SECRET do not save this client text", "details": "PRIVATE",
            }}).encode())
        return VisionHttpResponse(200, json.dumps({
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                "page": 1, "tables": [], "unreadable_fragments": [], "handwritten_replacements": [],
            })}}], "usage": {"prompt_tokens": 4000, "completion_tokens": 200,
                              "completion_tokens_details": {"reasoning_tokens": 3}},
        }).encode())


def send(run, case, fake, **kwargs):
    args = dict(allow_paid_api=True, confirm_live_preflight=True,
                accept_unverified_account_quotas=True, confirm_exclusive_key_use=True,
                api_key="synthetic-private-key", transport=fake)
    return ab.dispatch(run, case, **(args | kwargs))


def test_prepare_and_preflight_are_offline_and_freeze_exact_wire_bytes(bundle):
    plan, payloads, image, hint, report = ab.preflight(bundle)
    assert report["status"] == "dry_run_ready" and report["http_attempts"] == 0
    assert payloads["A"].request_bytes == payloads["B"].request_bytes
    assert payloads["B"].body.endswith(b" " * 31)
    assert report["reserved_total_tokens"] <= 60000
    assert Decimal(report["reserved_rub_pair"]) < Decimal("100")
    assert report["account_quota_status"] == "unconfirmed"
    assert report["account_rpm_verified"] is None
    assert plan.image_sha256 == ab._sha(image)
    assert hint not in json.dumps(report) and "synthetic-folder" not in json.dumps(report)
    assert not list(bundle.rglob("*transcription*"))
    assert not list(bundle.rglob("*intent*"))
    assert verify_private_tree(bundle) >= 5


@pytest.mark.parametrize("field,value", [
    ("budget", Decimal("1")), ("tpm", 49000), ("input_bound", 250000),
    ("input_bound", 0), ("rpm", 0), ("tpm", 0), ("budget", Decimal("NaN")),
])
def test_invalid_budget_context_and_local_quotas_block_preparation(inputs, field, value):
    inputs[field] = value
    with pytest.raises((ab.ABError, ValueError)):
        ab.prepare(**inputs)


@pytest.mark.parametrize("flag", ["allow_paid_api", "confirm_live_preflight",
                                 "accept_unverified_account_quotas", "confirm_exclusive_key_use"])
def test_each_explicit_permission_required_before_transport(bundle, flag):
    fake = Fake()
    with pytest.raises(ab.ABError, match="explicit_permission"):
        send(bundle, "B", fake, **{flag: False})
    assert fake.calls == []
    assert not list(bundle.glob("*intent*"))


def test_b_then_a_send_only_frozen_bytes_once_and_preserve_private_sidecars(bundle, monkeypatch):
    monkeypatch.setattr(ab._LiveInvocationGuard, "_check_cooldown", lambda _: None)
    _, payloads, _, hint, _ = ab.preflight(bundle)
    fake = Fake()
    b = send(bundle, "B", fake)
    a = send(bundle, "A", fake)
    assert b["outcome"] == a["outcome"] == "completed"
    assert b["usage"]["reasoning_tokens"] == 3
    assert len(fake.calls) == 2
    for (url, call), case in zip(fake.calls, ("B", "A")):
        assert url == "https://ai.api.cloud.yandex.net/v1/chat/completions"
        assert call["body"] == payloads[case].body
        data = json.loads(call["body"])
        assert data["reasoning_effort"] == "none" and data["max_completion_tokens"] == 32768
        sidecar = json.loads((bundle / f"{case}-diagnostics-v1.json").read_text())
        assert sidecar["evidence"]["system_message_count"] == (1 if case == "B" else 2)
        assert sidecar["evidence"]["request_body_bytes"] == len(call["body"])
        assert hint not in json.dumps(sidecar) and "synthetic-private-key" not in json.dumps(sidecar)
    for case in ("B", "A"):
        with pytest.raises(ab.ABError, match="already_consumed"):
            send(bundle, case, fake)
    assert len(fake.calls) == 2
    assert len(list(bundle.glob("*intent.json"))) == 2


def test_a_cannot_be_first_and_c_is_unavailable(bundle):
    fake = Fake()
    for case in ("A", "C"):
        with pytest.raises(ab.ABError):
            send(bundle, case, fake)
    assert fake.calls == []


def test_real_cooldown_blocks_second_case_without_http(bundle):
    fake = Fake()
    send(bundle, "B", fake)
    with pytest.raises(ControlledPilotError, match="cooldown_active"):
        send(bundle, "A", fake)
    assert len(fake.calls) == 1
    assert not (bundle / "A-intent.json").exists()


@pytest.mark.parametrize("status,timeout", [(429, False), (500, False), (502, False),
                                           (503, False), (504, False), (200, True)])
def test_uncertain_failure_consumes_slot_and_blocks_a_without_retry(bundle, monkeypatch, status, timeout):
    monkeypatch.setattr(ab._LiveInvocationGuard, "_check_cooldown", lambda _: None)
    fake = Fake(status=status, error=timeout)
    report = send(bundle, "B", fake)
    assert report["outcome"] == "unknown_outcome"
    assert report["estimated_cost_rub"] is None
    for case in ("B", "A"):
        with pytest.raises(ab.ABError):
            send(bundle, case, fake)
    assert len(fake.calls) == 1
    assert not list(bundle.glob("*transcription*"))


def test_400_code_is_saved_without_prose_or_unknown_cost_assumption(bundle, monkeypatch):
    monkeypatch.setattr(ab._LiveInvocationGuard, "_check_cooldown", lambda _: None)
    fake = Fake(status=400)
    report = send(bundle, "B", fake)
    assert report["outcome"] == "rejected" and report["estimated_cost_rub"] is None
    sidecar = json.loads((bundle / "B-diagnostics-v1.json").read_text())
    assert sidecar["evidence"]["provider_error_param"] == "messages"
    assert sidecar["evidence"]["provider_error_code"] == "unsupported_parameter"
    assert "SECRET" not in json.dumps(sidecar) and "PRIVATE" not in json.dumps(sidecar)
    assert (bundle / "B-intent.json").is_file()


@pytest.mark.parametrize("artifact", ["context.txt", "image.png", "source"])
def test_changed_artifacts_block_without_http(bundle, artifact):
    plan = ab.preflight(bundle)[0]
    path = Path(plan.source_path) if artifact == "source" else bundle / artifact
    with path.open("ab") as stream:
        stream.write(b"x")
    fake = Fake()
    with pytest.raises(ab.ABError, match="artifact_changed"):
        send(bundle, "B", fake)
    assert fake.calls == []


def test_changed_timeout_or_pricing_invalidates_frozen_plan(bundle, monkeypatch):
    monkeypatch.setattr(ab, "LIMITS", replace(ab.LIMITS, request_timeout_s=299))
    with pytest.raises(ab.ABError, match="configuration_changed"):
        ab.preflight(bundle)


def test_changing_credentials_between_cases_is_blocked(bundle, monkeypatch):
    monkeypatch.setattr(ab._LiveInvocationGuard, "_check_cooldown", lambda _: None)
    fake = Fake()
    send(bundle, "B", fake)
    with pytest.raises(ab.ABError, match="credentials_changed"):
        send(bundle, "A", fake, api_key="different-synthetic-key")
    assert len(fake.calls) == 1


def test_acl_failure_blocks_before_any_new_storage_or_request(bundle, monkeypatch):
    fake = Fake()
    def deny(_):
        raise PrivateAclError("private_acl_unverified")
    monkeypatch.setattr(ab, "verify_private_tree", deny)
    with pytest.raises(PrivateAclError):
        send(bundle, "B", fake)
    assert fake.calls == [] and not list(bundle.glob("*intent*"))


def test_lost_sidecar_keeps_consumed_intent_and_prevents_resend(bundle, monkeypatch):
    monkeypatch.setattr(ab._LiveInvocationGuard, "_check_cooldown", lambda _: None)
    original_write = ab._write_json_private
    def fail(path, value):
        if path.name.endswith("diagnostics-v1.json"):
            raise OSError("synthetic storage failure")
        original_write(path, value)
    monkeypatch.setattr(ab, "_write_json_private", fail)
    fake = Fake()
    with pytest.raises(OSError):
        send(bundle, "B", fake)
    with pytest.raises(ab.ABError, match="already_consumed"):
        send(bundle, "B", fake)
    with pytest.raises(ab.ABError, match="previous_outcome_unknown"):
        send(bundle, "A", fake)
    assert len(fake.calls) == 1


def test_interrupted_attempt_does_not_retry_or_advance(bundle, monkeypatch):
    monkeypatch.setattr(ab._LiveInvocationGuard, "_check_cooldown", lambda _: None)
    ab._write_json_private(bundle / "B-intent.json", {"outcome": "unknown_outcome"})
    fake = Fake()
    with pytest.raises(ab.ABError, match="already_consumed"):
        send(bundle, "B", fake)
    with pytest.raises((ab.ABError, FileNotFoundError)):
        send(bundle, "A", fake)
    assert fake.calls == []


def test_cancellation_before_dispatch_does_not_consume_slot(bundle):
    event, fake = threading.Event(), Fake()
    event.set()
    with pytest.raises(ab.ABError, match="cancelled_before_dispatch"):
        send(bundle, "B", fake, cancel=event)
    assert not list(bundle.glob("*intent*")) and fake.calls == []


def test_cli_without_flags_never_resolves_or_sends_a_key(bundle, capsys):
    assert ab.main(["run-case", "--run-dir", str(bundle), "--case", "B"]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "ab_explicit_permission_required"


def test_output_in_repository_is_rejected(inputs, tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(ab.ABError, match="output_inside_repository"):
        ab.prepare(**inputs)
