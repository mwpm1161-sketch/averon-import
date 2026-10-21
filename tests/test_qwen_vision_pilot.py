from __future__ import annotations

import json
import socket
import stat
from pathlib import Path

import fitz
import pytest

from averon_import.services.ocr.qwen_vision_client import _HttpsTransport, VisionHttpResponse
from averon_import.services.ocr.qwen_vision_contract import VisionPage
from averon_import.services.ocr.base import OcrResult, PageOcrResult
from averon_import.services.secrets import MemorySecretStore, YANDEX_AI_API_KEY
from averon_import.tools import qwen_vision_pilot as pilot


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("pilot dry-run attempted network access")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(_HttpsTransport, "post", denied)


@pytest.fixture
def synthetic_pdf(tmp_path):
    target = tmp_path / "synthetic-only.pdf"
    with fitz.open() as document:
        for index in range(2):
            page = document.new_page(width=72, height=72)
            page.insert_text((4, 20), f"SYNTHETIC PAGE {index + 1}")
        document.save(target)
    return target


def _settings(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.json").write_text(json.dumps({
        "yandex": {
            "folder_id": "test-folder",
            "llm_model": "gpt://test-folder/qwen3.6-35b-a3b/latest",
            "llm_base_url": "https://ai.api.cloud.yandex.net/v1",
        },
    }), encoding="utf-8")


def _args(tmp_path, synthetic_pdf, *extra):
    data_dir = tmp_path / "settings"
    output_dir = tmp_path / "private-output"
    _settings(data_dir)
    return pilot._parser().parse_args([
        "--pdf", str(synthetic_pdf), "--pages", "1-2", "--dpi", "72",
        "--max-requests", "2", "--data-dir", str(data_dir),
        "--output-dir", str(output_dir), *extra,
    ])


def _vision_page(number: int) -> VisionPage:
    return VisionPage.model_validate({
        "page": number,
        "tables": [{
            "headers": ["Поз.", "Наименование", "Количество"],
            "rows": [{
                "kind": "item", "logical_row_index": None, "continuation_of": None,
                "continues_previous_page": False, "struck_through": False, "handwritten": False,
                "handwritten_replacements": [], "bbox_candidate": None,
                "cells": [
                    {"column_index": 0, "column_span": 1, "row_span": 1, "raw_text": "1", "alternatives": [],
                     "handwritten_replacements": [], "unreadable": False, "struck_through": False, "handwritten": False,
                     "bbox_candidate": None},
                    {"column_index": 1, "column_span": 1, "row_span": 1, "raw_text": "Клапан", "alternatives": [],
                     "handwritten_replacements": [], "unreadable": False, "struck_through": False, "handwritten": False,
                     "bbox_candidate": None},
                    {"column_index": 2, "column_span": 1, "row_span": 1, "raw_text": "2", "alternatives": [],
                     "handwritten_replacements": [], "unreadable": False, "struck_through": False, "handwritten": False,
                     "bbox_candidate": None},
                ],
            }],
            "continues_previous_page": False, "bbox_candidate": None,
        }],
        "unreadable_fragments": [], "handwritten_replacements": [],
    })


def test_pages_and_crop_parse_strictly():
    assert pilot._pages("1,3-4") == [1, 3, 4]
    assert pilot._crop("0.1,0.2,0.5,0.6") == {"x": 0.1, "y": 0.2, "width": 0.5, "height": 0.6}
    with pytest.raises(Exception):
        pilot._pages("1,1")
    with pytest.raises(Exception):
        pilot._crop("0,0,2,1")


def test_default_dry_run_renders_and_measures_without_http(tmp_path, synthetic_pdf):
    args = _args(tmp_path, synthetic_pdf)
    code, run_dir, report = pilot.run(args)

    assert code == 0
    assert report["status"] == "dry_run_ready"
    assert report["mode"] == "dry_run"
    assert report["max_output_tokens"] == 8192
    assert report["request_timeout_s"] == 120
    assert report["configuration"]["max_output_tokens"] == 8192
    assert report["configuration"]["max_output_tokens_cap"] == 32768
    assert report["configuration"]["request_timeout_s"] == 120
    assert report["configuration"]["total_timeout_s"] == 600
    assert report["candidate_row_count"] is None
    assert report["configuration"]["credential_status"] == "not_inspected_during_dry_run"
    assert report["configuration"]["requests_planned"] == 2
    assert [page["page"] for page in report["pages"]] == [1, 2]
    assert all(page["image_bytes"] > 0 and page["request_body_bytes"] > page["image_bytes"] for page in report["pages"])
    assert report["actual_usage"]["prompt_tokens"] is None
    assert report["cost_estimate"]["estimate_rub"] is None
    assert (run_dir / "report.json").is_file()
    assert not (run_dir / "transcription.json").exists()
    assert "SYNTHETIC PAGE" not in (run_dir / "report.json").read_text(encoding="utf-8")
    if __import__("os").name != "nt":
        assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700


@pytest.mark.parametrize("limit", [16384, 24576, 32768])
def test_dry_run_reports_selected_output_limit_and_makes_no_http_request(tmp_path, synthetic_pdf, limit):
    args = _args(tmp_path, synthetic_pdf, "--max-output-tokens", str(limit), "--disable-reasoning")
    code, _, report = pilot.run(args)
    assert code == 0 and report["status"] == "dry_run_ready"
    assert report["max_output_tokens"] == limit
    assert report["configuration"]["max_output_tokens"] == limit
    assert report["configuration"]["reasoning_effort"] == "none"


@pytest.mark.parametrize("timeout", [120, 300, 0.25])
def test_dry_run_reports_request_timeout_and_makes_no_http_request(tmp_path, synthetic_pdf, timeout):
    args = _args(tmp_path, synthetic_pdf, "--request-timeout-s", str(timeout))
    code, _, report = pilot.run(args)
    assert code == 0 and report["status"] == "dry_run_ready"
    assert report["request_timeout_s"] == timeout
    assert report["configuration"]["request_timeout_s"] == timeout
    assert report["configuration"]["total_timeout_s"] == 600


@pytest.mark.parametrize("value", ["0", "-1", "300.001", "301", "nan", "inf", "-inf", "abc"])
def test_cli_rejects_invalid_request_timeouts(value):
    with pytest.raises(SystemExit):
        pilot._parser().parse_args([
            "--pdf", "input.pdf", "--pages", "1", "--max-requests", "1",
            "--request-timeout-s", value,
        ])


@pytest.mark.parametrize("value", ["0", "-1", "32769", "1.5", "abc"])
def test_cli_rejects_invalid_output_token_limits(value):
    with pytest.raises(SystemExit):
        pilot._parser().parse_args([
            "--pdf", "input.pdf", "--pages", "1", "--max-requests", "1",
            "--max-output-tokens", value,
        ])


def test_mocked_live_flow_saves_transcription_reference_and_usage_without_http(
    tmp_path, synthetic_pdf, monkeypatch,
):
    reference_csv = tmp_path / "reference.csv"
    reference_csv.write_text(
        "page,table_index,row_index,position,name,quantity\n"
        "1,0,0,1,Клапан,2\n"
        "2,0,0,1,Клапан,2\n",
        encoding="utf-8",
    )
    args = _args(
        tmp_path, synthetic_pdf,
        "--reference-csv", str(reference_csv), "--allow-paid-api",
        "--input-rub-per-1k", "2", "--output-rub-per-1k", "4",
    )
    store = MemorySecretStore()
    store.set(YANDEX_AI_API_KEY, "mock-only-key")
    monkeypatch.setattr(pilot, "create_secret_store", lambda *_args, **_kwargs: store)

    class FakeProvider:
        def __init__(self, config):
            assert config.configured

        def recognize(self, pdf_path, pages, *, transcription_callback, **kwargs):
            page_results = []
            for page in pages:
                transcription_callback(page, _vision_page(page))
                page_results.append(PageOcrResult(page=page, stats={
                    "request_attempted": True,
                    "elapsed_ms": 12.5,
                    "finish_reason": "stop",
                    "http_status": 200,
                    "usage": {
                        "prompt_tokens": 100 * page,
                        "completion_tokens": 20 * page,
                        "total_tokens": 120 * page,
                        **({"completion_tokens_details": {"reasoning_tokens": 7}} if page == 1 else {}),
                    },
                }))
            return OcrResult("qwen_vision", pages=page_results, stats={"elapsed_ms": 31.0})

    monkeypatch.setattr(pilot, "QwenVisionProvider", FakeProvider)
    code, run_dir, report = pilot.run(args)

    assert code == 0
    assert report["status"] == "completed"
    assert report["candidate_row_count"] == 2
    assert report["actual_usage"]["prompt_tokens"] == 300
    assert report["actual_usage"]["completion_tokens"] == 60
    assert report["actual_usage"]["pages"][0]["usage"]["completion_tokens_details"]["reasoning_tokens"] == 7
    assert report["actual_usage"]["pages"][1]["usage"]["completion_tokens_details"]["reasoning_tokens"] is None
    assert report["cost_estimate"]["estimate_rub"] == "0.84"
    assert report["reference_comparison"]["equal_text_locations"] == 2
    assert (run_dir / "transcription.json").is_file()
    assert (run_dir / "reference_comparison.json").is_file()
    semantic = json.loads((run_dir / "semantic_review.json").read_text(encoding="utf-8"))
    assert semantic["summary"]["source_row_count"] == 2
    assert semantic["summary"]["potential_numbered_position_count"] == 2
    assert all(row["role_verified"] is False for row in semantic["rows"])
    assert all(row["projection"]["diagnostic_is_admission"] is False for row in semantic["rows"])
    assert "Клапан" not in (run_dir / "report.json").read_text(encoding="utf-8")

    assert "mock-only-key" not in (run_dir / "report.json").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "second_response,expected_error",
    [
        (VisionHttpResponse(500, b"private upstream response must not be logged"), "http_error"),
        (TimeoutError("private upstream timeout"), "timeout"),
    ],
    ids=["http-500", "request-timeout"],
)
def test_request_failure_after_success_preserves_partial_result_usage_and_fails_exit(
    tmp_path, synthetic_pdf, monkeypatch, capsys, second_response, expected_error,
):
    args = _args(
        tmp_path, synthetic_pdf, "--allow-paid-api",
        "--input-rub-per-1k", "2", "--output-rub-per-1k", "4",
    )
    store = MemorySecretStore()
    store.set(YANDEX_AI_API_KEY, "mock-only-key")
    monkeypatch.setattr(pilot, "create_secret_store", lambda *_args, **_kwargs: store)
    success = json.dumps({
        "choices": [{
            "finish_reason": "stop",
            "message": {"content": json.dumps(_vision_page(1).model_dump(mode="json"), ensure_ascii=False)},
        }],
        "usage": {"prompt_tokens": 101, "completion_tokens": 22, "total_tokens": 123},
    }, ensure_ascii=False).encode("utf-8")
    responses = iter((VisionHttpResponse(200, success), second_response))
    calls = []

    def mocked_post(*_args, **kwargs):
        calls.append(kwargs)
        value = next(responses)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(_HttpsTransport, "post", mocked_post)

    code = pilot.main([
        "--pdf", str(args.pdf), "--pages", "1-2", "--dpi", "72",
        "--max-requests", "2", "--request-timeout-s", "300",
        "--data-dir", str(args.data_dir), "--output-dir", str(args.output_dir), "--allow-paid-api",
        "--input-rub-per-1k", "2", "--output-rub-per-1k", "4",
    ])
    cli_result = json.loads(capsys.readouterr().out)
    run_dir = Path(cli_result["run_directory"])
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))

    assert code != 0
    assert cli_result["status"] == "completed_with_errors"
    assert report["status"] == "completed_with_errors"
    assert report["request_timeout_s"] == 300
    assert report["configuration"]["request_timeout_s"] == 300
    assert report["configuration"]["total_timeout_s"] == 600
    assert report["pages"][1]["error_codes"] == [expected_error]
    assert len(calls) == 2  # One request per page; no automatic retry after the failure.
    assert report["pages"][0]["usage"]["prompt_tokens"] == 101
    assert report["pages"][0]["usage"]["completion_tokens_details"]["reasoning_tokens"] is None
    assert report["pages"][1]["usage"]["prompt_tokens"] is None
    assert report["actual_usage"]["prompt_tokens"] is None
    assert report["actual_usage"]["reported_subtotals"]["prompt_tokens"] == 101
    assert report["actual_usage"]["reported_request_counts"]["prompt_tokens"] == 1
    assert report["cost_estimate"]["status"] == "unknown_incomplete_api_usage"
    assert report["cost_estimate"]["estimate_rub"] is None
    transcription = json.loads((run_dir / "transcription.json").read_text(encoding="utf-8"))
    assert [page["page"] for page in transcription["pages"]] == [1]
    diagnostic_text = (run_dir / "report.json").read_text(encoding="utf-8")
    assert "private upstream response" not in diagnostic_text
    assert "private upstream timeout" not in diagnostic_text
    assert "mock-only-key" not in diagnostic_text


def test_invalid_pdf_is_reported_without_document_content(tmp_path):
    invalid_pdf = tmp_path / "not-a-pdf.pdf"
    invalid_pdf.write_bytes(b"not a pdf; private marker")
    args = _args(tmp_path, invalid_pdf)
    code, run_dir, report = pilot.run(args)
    assert code == 1
    assert report["status"] == "preflight_failed"
    assert "invalid_pdf" in report["errors"]
    assert "private marker" not in (run_dir / "report.json").read_text(encoding="utf-8")


def test_live_option_has_a_mandatory_request_limit():
    with pytest.raises(SystemExit):
        pilot._parser().parse_args(["--pdf", "input.pdf", "--pages", "1", "--allow-paid-api"])


def test_reference_comparison_reports_text_only_and_unmatched_locations():
    transcriptions = {1: _vision_page(1)}
    candidates = pilot._candidate_rows(transcriptions)
    reference = [{
        "page": "1", "table_index": "0", "row_index": "0",
        "position": "1", "name": "КЛАПАН", "quantity": "3",
    }]
    result = pilot._compare_reference(reference, candidates)
    assert result["interpretation"].endswith("does not establish correctness.")
    assert result["counts"] == {
        "paired_locations": 1,
        "equal_text_locations": 0,
        "different_text_locations": 1,
        "reference_without_candidate": 0,
        "candidate_without_reference": 0,
    }
    assert result["pairs"][0]["equal_fields"] == ["name", "position"]
    assert result["pairs"][0]["different_fields"] == ["quantity"]
    assert not {"correct", "is_correct", "approved"} & {
        key.casefold() for pair in result["pairs"] for key in pair
    }


def test_cost_estimate_keeps_missing_usage_unknown():
    missing = pilot._cost_estimate([], None, None, None)
    assert missing["status"] == "no_requests_attempted"
    partial = pilot._cost_estimate([
        {"request_attempted": True, "usage": {"prompt_tokens": None, "completion_tokens": None}},
    ], pilot.Decimal("1"), pilot.Decimal("2"), None)
    assert partial["status"] == "unknown_incomplete_api_usage"
    assert partial["estimate_rub"] is None


def test_usage_aggregates_known_metrics_without_filling_unknown_ones():
    usage = pilot._aggregate_usage([
        {"page": 1, "request_attempted": True,
         "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": None}},
        {"page": 2, "request_attempted": True,
         "usage": {"prompt_tokens": 200, "completion_tokens": 30, "total_tokens": 230}},
    ])
    assert usage["prompt_tokens"] == 300
    assert usage["completion_tokens"] == 50
    assert usage["total_tokens"] is None
    assert usage["metric_complete_for_attempted_requests"] == {
        "prompt_tokens": True, "completion_tokens": True, "total_tokens": False,
    }
    assert usage["reported_subtotals"] == {
        "prompt_tokens": 300, "completion_tokens": 50, "total_tokens": 230,
    }


def test_cost_estimate_uses_reported_tokens_and_tariff():
    result = pilot._cost_estimate([
        {"request_attempted": True, "usage": {"prompt_tokens": 1500, "completion_tokens": 500}},
    ], pilot.Decimal("2"), pilot.Decimal("4"), None)
    assert result["status"] == "estimated_from_reported_usage"
    assert result["estimate_rub"] == "5.00"
