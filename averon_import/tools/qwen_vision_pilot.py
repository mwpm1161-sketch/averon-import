"""Isolated, opt-in diagnostic CLI for the experimental Qwen Vision provider.

This command is deliberately not registered in the application runtime. Dry-run
renders and sizes pages locally; only ``--allow-paid-api`` can call the provider.
"""

from __future__ import annotations

import argparse
import csv
from io import BytesIO
import json
import math
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import fitz
from PIL import Image

from averon_import.services.app_settings import AppSettingsService
from averon_import.services.ocr.qwen_vision import QwenVisionProvider
from averon_import.services.ocr.qwen_vision_client import (
    QwenVisionClient,
    QwenVisionConfig,
    QwenVisionError,
    MAX_OUTPUT_TOKENS_CAP,
    VisionLimits,
)
from averon_import.services.ocr.qwen_vision_contract import VisionPage
from averon_import.services.ocr.qwen_vision_diagnostics import analyze_transcriptions
from averon_import.services.pdf_service import PdfService
from averon_import.services.secrets import MemorySecretStore, create_secret_store


_BASE_LIMITS = VisionLimits()
MAX_REQUEST_TIMEOUT_S = 300.0
_CANONICAL_FIELDS = (
    "position", "name", "type_mark", "code", "manufacturer", "unit",
    "quantity", "mass", "note",
)
_KEY_RE = re.compile(r"^S-\d-(?:\d+-)*\d+$")


class PilotError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _default_data_dir() -> Path:
    configured = os.environ.get("AVERON_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        root = Path(local) if local else Path.home() / "AppData" / "Local"
        return (root / "Averon Import" / "data").resolve()
    return (Path.cwd() / "data").resolve()


def _default_output_root() -> Path:
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        root = Path(local) if local else Path.home() / "AppData" / "Local"
        return root / "Averon Import" / "vision-pilot"
    state = os.environ.get("XDG_STATE_HOME", "").strip()
    root = Path(state) if state else Path.home() / ".local" / "state"
    return root / "averon-import" / "vision-pilot"


def _pages(value: str) -> list[int]:
    result: list[int] = []
    try:
        for part in value.split(","):
            part = part.strip()
            if not part:
                raise ValueError
            if "-" in part:
                start_text, stop_text = part.split("-", 1)
                start, stop = int(start_text), int(stop_text)
                if start < 1 or stop < start or stop - start >= _BASE_LIMITS.max_pages:
                    raise ValueError
                result.extend(range(start, stop + 1))
            else:
                result.append(int(part))
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("use page numbers and ranges, for example 1,3-4") from None
    if not result or any(page < 1 for page in result) or len(result) != len(set(result)):
        raise argparse.ArgumentTypeError("pages must be positive and unique")
    if len(result) > _BASE_LIMITS.max_pages:
        raise argparse.ArgumentTypeError(f"at most {_BASE_LIMITS.max_pages} pages may be selected")
    return sorted(result)


def _crop(value: str | None) -> dict[str, float] | None:
    if value is None:
        return None
    try:
        values = [float(part.strip()) for part in value.split(",")]
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("crop must be x,y,width,height") from None
    if len(values) != 4 or any(not math.isfinite(item) for item in values):
        raise argparse.ArgumentTypeError("crop must be x,y,width,height")
    x, y, width, height = values
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
        raise argparse.ArgumentTypeError("crop must fit within normalized image coordinates")
    return {"x": x, "y": y, "width": width, "height": height}


def _identity_sid() -> str:
    try:
        result = subprocess.run(
            ["whoami.exe", "/user", "/fo", "csv", "/nh"],
            check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=10,
        )
        records = list(csv.reader(result.stdout.splitlines()))
        sid = records[0][-1].strip() if records else ""
    except Exception:
        raise PilotError("secure_output_identity_unavailable") from None
    if not _KEY_RE.fullmatch(sid):
        raise PilotError("secure_output_identity_unavailable")
    return sid


def _secure_run_directory(root: Path) -> Path:
    root = root.expanduser().resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = root / f"run-{stamp}-{uuid.uuid4().hex[:12]}"
        run_dir.mkdir(mode=0o700)
    except OSError:
        raise PilotError("output_directory_unavailable") from None
    if os.name == "nt":
        try:
            subprocess.run(
                [
                    "icacls.exe", str(run_dir), "/inheritance:r", "/grant:r",
                    f"*{_identity_sid()}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F",
                    "*S-1-5-32-544:(OI)(CI)F",
                ],
                check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL, timeout=15,
            )
        except PilotError:
            try:
                run_dir.rmdir()
            except OSError:
                pass
            raise
        except Exception:
            try:
                run_dir.rmdir()
            except OSError:
                pass
            raise PilotError("secure_output_acl_failed") from None
    else:
        try:
            os.chmod(run_dir, 0o700)
            if run_dir.stat().st_mode & 0o077:
                raise OSError("permissions not private")
        except OSError:
            try:
                run_dir.rmdir()
            except OSError:
                pass
            raise PilotError("secure_output_permissions_failed") from None
    return run_dir


def _write_json_private(path: Path, payload: Any) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
        if os.name != "nt":
            os.chmod(path, 0o600)
    except (OSError, TypeError, ValueError):
        raise PilotError("output_write_failed") from None


def _config(settings: Any, limits: VisionLimits) -> QwenVisionConfig:
    # Use only non-secret environment overrides here; the credential is never
    # inspected in dry-run. Live resolution happens after the explicit flag.
    environ = {
        key: os.environ[key]
        for key in ("AVERON_YANDEX_AI_BASE_URL", "AVERON_YANDEX_AI_MODEL")
        if key in os.environ
    }
    try:
        return QwenVisionConfig.from_app_settings(
            settings.yandex, MemorySecretStore(), limits=limits, environ=environ,
        )
    except Exception:
        raise PilotError("invalid_ai_configuration") from None


def _validate_target_config(config: QwenVisionConfig) -> None:
    if "qwen3.6-35b-a3b" not in config.model.casefold():
        raise PilotError("unexpected_model_configuration")
    if urlsplit(config.base_url).hostname != "ai.api.cloud.yandex.net":
        raise PilotError("unexpected_ai_endpoint")


def _render_and_measure(
    pdf_path: Path, pages: list[int], dpi: int, crop: dict[str, float] | None,
    config: QwenVisionConfig,
) -> list[dict[str, Any]]:
    pdf = PdfService()
    client = QwenVisionClient(config)
    results: list[dict[str, Any]] = []
    try:
        with fitz.open(pdf_path) as document:
            page_count = document.page_count
            if not page_count or any(page > page_count for page in pages):
                raise PilotError("invalid_page_selection")
            for page_number in pages:
                started = time.perf_counter()
                rect = document[page_number - 1].rect
                width = math.ceil(rect.width * dpi / 72)
                height = math.ceil(rect.height * dpi / 72)
                if width * height > config.limits.max_image_pixels:
                    results.append({"page": page_number, "error_code": "image_pixel_limit"})
                    continue
                image = pdf.render_page(pdf_path, page_number, dpi=dpi)
                if len(image) > config.limits.max_image_bytes:
                    results.append({"page": page_number, "error_code": "image_size_limit"})
                    continue
                with Image.open(BytesIO(image)) as opened:
                    image_width, image_height = opened.size
                    if opened.width * opened.height > config.limits.max_image_pixels:
                        results.append({"page": page_number, "error_code": "image_pixel_limit"})
                        continue
                    if crop is not None:
                        left = int(crop["x"] * opened.width)
                        top = int(crop["y"] * opened.height)
                        right = int((crop["x"] + crop["width"]) * opened.width)
                        bottom = int((crop["y"] + crop["height"]) * opened.height)
                        if right <= left or bottom <= top:
                            results.append({"page": page_number, "error_code": "invalid_crop"})
                            continue
                        buffer = BytesIO()
                        opened.crop((left, top, right, bottom)).save(buffer, format="PNG")
                        image = buffer.getvalue()
                if len(image) > config.limits.max_image_bytes:
                    results.append({"page": page_number, "error_code": "image_size_limit"})
                    continue
                try:
                    request_bytes = client.measure_request_bytes(image, page_number)
                except QwenVisionError as exc:
                    results.append({"page": page_number, "error_code": exc.code})
                    continue
                if request_bytes > config.limits.max_request_bytes:
                    results.append({"page": page_number, "error_code": "request_size_limit"})
                    continue
                with Image.open(BytesIO(image)) as final_image:
                    final_width, final_height = final_image.size
                results.append({
                    "page": page_number,
                    "image_width": final_width,
                    "image_height": final_height,
                    "image_bytes": len(image),
                    "request_body_bytes": request_bytes,
                    "preflight_elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                })
    except PilotError:
        raise
    except Exception:
        raise PilotError("invalid_pdf") from None
    return results


def _read_reference(path: Path | None) -> list[dict[str, str]] | None:
    if path is None:
        return None
    try:
        with path.expanduser().open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            headers = [str(item or "").strip().casefold() for item in (reader.fieldnames or [])]
            required = {"page", "table_index", "row_index"}
            if len(headers) != len(set(headers)) or not required.issubset(headers):
                raise ValueError
            compared = set(headers) & set(_CANONICAL_FIELDS)
            if not compared:
                raise ValueError
            rows: list[dict[str, str]] = []
            seen: set[tuple[int, int, int]] = set()
            for raw in reader:
                if raw is None:
                    raise ValueError
                normalized = {str(key or "").strip().casefold(): str(value or "") for key, value in raw.items()}
                key = (int(normalized["page"]), int(normalized["table_index"]), int(normalized["row_index"]))
                if key[0] < 1 or key[1] < 0 or key[2] < 0 or key in seen:
                    raise ValueError
                if len(rows) >= _BASE_LIMITS.max_pages * _BASE_LIMITS.max_rows_per_page:
                    raise ValueError
                seen.add(key)
                rows.append({**normalized, "page": str(key[0]), "table_index": str(key[1]), "row_index": str(key[2])})
            return rows
    except Exception:
        raise PilotError("invalid_reference_csv") from None


def _candidate_rows(transcriptions: dict[int, VisionPage]) -> list[dict[str, Any]]:
    """Return source-preserving rows with unverified structural hypotheses."""
    return analyze_transcriptions(transcriptions)["rows"]


def _normalized(value: str) -> str:
    return " ".join(str(value).casefold().split())


def _compare_reference(
    reference: list[dict[str, str]], candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    candidate_map = {
        (row["page"], row["table_index"], row["row_index"]): row
        for row in candidates
    }
    reference_map = {
        (int(row["page"]), int(row["table_index"]), int(row["row_index"])): row
        for row in reference
    }
    fields = sorted(set(reference[0]) & set(_CANONICAL_FIELDS)) if reference else []
    compared_pairs = []
    for key in sorted(set(candidate_map) & set(reference_map)):
        candidate = candidate_map[key]
        expected = reference_map[key]
        equal_fields = [field for field in fields if _normalized(candidate["values"].get(field, "")) == _normalized(expected.get(field, ""))]
        different_fields = [field for field in fields if field not in equal_fields]
        compared_pairs.append({
            "page": key[0], "table_index": key[1], "row_index": key[2],
            "text_comparison": "equal_after_whitespace_and_case_normalization" if not different_fields else "different_text",
            "equal_fields": equal_fields, "different_fields": different_fields,
            "candidate_values": {field: candidate["values"].get(field, "") for field in fields},
            "reference_values": {field: expected.get(field, "") for field in fields},
        })
    reference_only = sorted(set(reference_map) - set(candidate_map))
    candidate_only = sorted(set(candidate_map) - set(reference_map))
    return {
        "format": "averon-qwen-vision-reference-comparison-v1",
        "interpretation": "Location and text comparison only. Text equality does not establish correctness.",
        "alignment_basis": "exact page/table_index/row_index supplied by the prepared reference; no fuzzy alignment",
        "compared_fields": fields,
        "pairs": compared_pairs,
        "reference_without_candidate": [list(key) for key in reference_only],
        "candidate_without_reference": [list(key) for key in candidate_only],
        "counts": {
            "paired_locations": len(compared_pairs),
            "equal_text_locations": sum(not item["different_fields"] for item in compared_pairs),
            "different_text_locations": sum(bool(item["different_fields"]) for item in compared_pairs),
            "reference_without_candidate": len(reference_only),
            "candidate_without_reference": len(candidate_only),
        },
    }


def _decimal_rate(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        rate = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("tariffs must be decimal numbers") from None
    if not rate.is_finite() or rate < 0:
        raise argparse.ArgumentTypeError("tariffs must be finite and non-negative")
    return rate


def _max_output_tokens(value: str) -> int:
    try:
        tokens = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if str(tokens) != value.strip() or not 1 <= tokens <= MAX_OUTPUT_TOKENS_CAP:
        raise argparse.ArgumentTypeError(
            f"must be an integer between 1 and {MAX_OUTPUT_TOKENS_CAP}"
        )
    return tokens


def _request_timeout_s(value: str) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a finite positive number") from None
    if not math.isfinite(seconds) or seconds <= 0 or seconds > MAX_REQUEST_TIMEOUT_S:
        raise argparse.ArgumentTypeError(f"must be finite, positive, and at most {MAX_REQUEST_TIMEOUT_S:g}")
    return seconds


def _cost_estimate(
    usage_pages: list[dict[str, Any]], input_rate: Decimal | None,
    output_rate: Decimal | None, cached_rate: Decimal | None,
) -> dict[str, Any]:
    attempted = [item for item in usage_pages if item.get("request_attempted")]
    tariff = {
        "currency": "RUB",
        "input_rub_per_1k_tokens": str(input_rate) if input_rate is not None else None,
        "cached_input_rub_per_1k_tokens": str(cached_rate) if cached_rate is not None else None,
        "output_rub_per_1k_tokens": str(output_rate) if output_rate is not None else None,
    }
    if not attempted:
        return {"status": "no_requests_attempted", "estimate_rub": "0.00", "tariff": tariff}
    if input_rate is None or output_rate is None:
        return {"status": "unknown_missing_tariff", "estimate_rub": None, "tariff": tariff}
    if any(item.get("usage", {}).get("prompt_tokens") is None or item.get("usage", {}).get("completion_tokens") is None for item in attempted):
        return {"status": "unknown_incomplete_api_usage", "estimate_rub": None, "tariff": tariff}
    if cached_rate is not None and any(item.get("usage", {}).get("prompt_tokens_details", {}).get("cached_tokens") is None for item in attempted):
        return {"status": "unknown_cached_token_usage", "estimate_rub": None, "tariff": tariff}
    input_total = Decimal(0)
    output_total = Decimal(0)
    for item in attempted:
        usage = item["usage"]
        prompt = usage["prompt_tokens"]
        completion = usage["completion_tokens"]
        if cached_rate is None:
            input_total += Decimal(prompt) * input_rate / Decimal(1000)
        else:
            cached = usage["prompt_tokens_details"]["cached_tokens"]
            if cached > prompt:
                return {"status": "unknown_inconsistent_api_usage", "estimate_rub": None, "tariff": tariff}
            input_total += Decimal(prompt - cached) * input_rate / Decimal(1000)
            input_total += Decimal(cached) * cached_rate / Decimal(1000)
        output_total += Decimal(completion) * output_rate / Decimal(1000)
    estimate = (input_total + output_total).quantize(Decimal("0.01"))
    return {"status": "estimated_from_reported_usage", "estimate_rub": str(estimate), "tariff": tariff}


def _aggregate_usage(usage_pages: list[dict[str, Any]]) -> dict[str, Any]:
    attempted = [item for item in usage_pages if item.get("request_attempted")]
    metrics = ("prompt_tokens", "completion_tokens", "total_tokens")
    complete = {
        key: bool(attempted) and all(item["usage"].get(key) is not None for item in attempted)
        for key in metrics
    }
    reported = {
        key: [item["usage"][key] for item in attempted if item["usage"].get(key) is not None]
        for key in metrics
    }
    return {
        "status": "reported" if attempted else "no_requests_attempted",
        "pages": usage_pages,
        "metric_complete_for_attempted_requests": {
            key: value if attempted else None for key, value in complete.items()
        },
        "usage_complete_for_attempted_requests": all(complete.values()) if attempted else None,
        # Keep known partial totals separately; primary totals remain unknown
        # whenever any attempted request omitted that metric.
        "reported_subtotals": {
            key: sum(values) if values else None for key, values in reported.items()
        },
        "reported_request_counts": {key: len(values) for key, values in reported.items()},
        **{
            key: sum(item["usage"][key] for item in attempted) if complete[key] else None
            for key in metrics
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline-first isolated Qwen Vision pilot")
    parser.add_argument("--pdf", type=Path, required=True, help="Local PDF path")
    parser.add_argument("--pages", type=_pages, required=True, help="Pages, e.g. 1,3-4")
    parser.add_argument("--dpi", type=int, default=150, help=f"Render DPI (1..{_BASE_LIMITS.dpi})")
    parser.add_argument(
        "--max-output-tokens", type=_max_output_tokens,
        default=_BASE_LIMITS.max_output_tokens,
        help=f"Maximum generated output tokens (1..{MAX_OUTPUT_TOKENS_CAP}; default 8192)",
    )
    parser.add_argument(
        "--request-timeout-s", type=_request_timeout_s,
        default=_BASE_LIMITS.request_timeout_s,
        help=f"Per-request timeout in seconds (>0..{MAX_REQUEST_TIMEOUT_S:g}; default 120)",
    )
    parser.add_argument(
        "--disable-reasoning", action="store_true",
        help="Send reasoning_effort=none (opt-in; no automatic fallback)",
    )
    parser.add_argument("--crop", type=_crop, help="Optional normalized crop x,y,width,height")
    parser.add_argument("--max-requests", type=int, required=True, help="Hard request cap (1..20); must cover selected pages")
    parser.add_argument("--allow-paid-api", action="store_true", help="Explicitly enable live API requests")
    parser.add_argument("--data-dir", type=Path, default=_default_data_dir(), help="Existing Averon settings/credential directory")
    parser.add_argument("--output-dir", type=Path, default=_default_output_root(), help="Protected local output root")
    parser.add_argument("--reference-csv", type=Path, help="Prepared CSV with page,table_index,row_index and canonical fields")
    parser.add_argument("--input-rub-per-1k", type=_decimal_rate, help="Input token tariff in RUB per 1,000 tokens")
    parser.add_argument("--cached-input-rub-per-1k", type=_decimal_rate, help="Cached input tariff in RUB per 1,000 tokens")
    parser.add_argument("--output-rub-per-1k", type=_decimal_rate, help="Output token tariff in RUB per 1,000 tokens")
    return parser


def run(args: argparse.Namespace) -> tuple[int, Path, dict[str, Any]]:
    run_dir = _secure_run_directory(args.output_dir)
    started = time.perf_counter()
    report: dict[str, Any] = {
        "format": "averon-qwen-vision-pilot-report-v1",
        "mode": "live" if args.allow_paid_api else "dry_run",
        "max_output_tokens": args.max_output_tokens,
        "request_timeout_s": args.request_timeout_s,
        "status": "preflight_failed",
        "pages": [],
        "candidate_row_count": None,
        "model_kind_candidate_row_count": None,
        "all_structured_row_count": None,
        "row_diagnostics": None,
        "errors": [],
        "duration_ms": None,
        "actual_usage": None,
        "cost_estimate": None,
    }
    try:
        pages = args.pages
        if args.dpi < 1 or args.dpi > _BASE_LIMITS.dpi:
            raise PilotError("dpi_limit")
        if args.max_requests < 1 or args.max_requests > _BASE_LIMITS.max_requests:
            raise PilotError("request_budget_limit")
        if len(pages) > args.max_requests:
            raise PilotError("request_budget_below_selected_pages")
        limits = VisionLimits(
            max_requests=args.max_requests,
            dpi=args.dpi,
            max_output_tokens=args.max_output_tokens,
            request_timeout_s=args.request_timeout_s,
            total_timeout_s=_BASE_LIMITS.total_timeout_s,
            reasoning_effort="none" if args.disable_reasoning else None,
        )
        settings_service = AppSettingsService(args.data_dir)
        dry_config = _config(settings_service.settings, limits)
        if not dry_config.model:
            raise PilotError("missing_model_configuration")
        _validate_target_config(dry_config)
        reference = _read_reference(args.reference_csv)
        if args.reference_csv is not None:
            report["reference_validation"] = {
                "provided": True,
                "row_count": len(reference or []),
                "comparison_is_correctness_claim": False,
            }
        preflight_pages = _render_and_measure(args.pdf.expanduser(), pages, args.dpi, args.crop, dry_config)
        report["pages"] = preflight_pages
        report["page_count"] = None
        try:
            with fitz.open(args.pdf.expanduser()) as document:
                report["page_count"] = document.page_count
        except Exception:
            raise PilotError("invalid_pdf") from None
        page_errors = [item["error_code"] for item in preflight_pages if item.get("error_code")]
        if page_errors:
            report["errors"].extend(page_errors)
            raise PilotError("page_preflight_failed")
        report["configuration"] = {
            "model_configured": bool(dry_config.model),
            "model_family": "qwen3.6-35b-a3b",
            "https_endpoint_valid": True,
            "credential_status": "not_inspected_during_dry_run" if not args.allow_paid_api else "pending_explicit_live_resolution",
            "maximum_requests": args.max_requests,
            "requests_planned": len(pages),
            "max_output_tokens": limits.max_output_tokens,
            "max_output_tokens_cap": MAX_OUTPUT_TOKENS_CAP,
            "request_timeout_s": limits.request_timeout_s,
            "total_timeout_s": limits.total_timeout_s,
            "reasoning_effort": limits.reasoning_effort,
            "image_and_request_limits_passed": True,
        }
        if not args.allow_paid_api:
            report["status"] = "dry_run_ready"
            report["errors"] = []
            report["actual_usage"] = {
                "status": "not_applicable_no_api_request",
                "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
            }
            report["cost_estimate"] = {
                "status": "not_applicable_no_api_request", "estimate_rub": None,
            }
        else:
            live_config = QwenVisionConfig.from_app_settings(
                settings_service.settings.yandex,
                create_secret_store(args.data_dir),
                limits=limits,
            )
            _validate_target_config(live_config)
            if not live_config.configured:
                raise PilotError("missing_live_credentials_or_model")
            report["configuration"]["credential_status"] = "available_for_this_explicit_live_run"
            provider = QwenVisionProvider(live_config)
            transcriptions: dict[int, VisionPage] = {}
            result = provider.recognize(
                args.pdf.expanduser(), pages, dpi=args.dpi, crop=args.crop,
                transcription_callback=lambda page, value: transcriptions.__setitem__(page, value),
            )
            transcription_payload = {
                "format": "averon-qwen-vision-transcription-v1",
                "pages": [
                    {"page": page, "transcription": value.model_dump(mode="json")}
                    for page, value in sorted(transcriptions.items())
                ],
            }
            if transcriptions:
                _write_json_private(run_dir / "transcription.json", transcription_payload)
            semantic_review = analyze_transcriptions(transcriptions)
            candidates = semantic_review["rows"]
            summary = semantic_review["summary"]
            report["candidate_row_count"] = (
                summary["potential_numbered_position_count"]
                + summary["potential_continuation_count"]
            )
            report["model_kind_candidate_row_count"] = summary["model_kind_candidate_row_count"]
            report["all_structured_row_count"] = summary["source_row_count"]
            report["row_diagnostics"] = {
                key: value for key, value in summary.items()
                if key != "position_sequences"
            }
            _write_json_private(
                run_dir / "semantic_review.json",
                {"format": "averon-qwen-vision-semantic-review-v1", **semantic_review},
            )
            usage_pages = []
            page_reports = []
            for page_result in result.pages:
                usage = page_result.stats.get("usage") if isinstance(page_result.stats.get("usage"), dict) else {}
                request_attempted = page_result.stats.get("request_attempted") is True
                safe_usage = {
                    key: usage.get(key) if type(usage.get(key)) is int else None
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                }
                details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
                safe_usage["prompt_tokens_details"] = {
                    "cached_tokens": details.get("cached_tokens") if type(details.get("cached_tokens")) is int else None
                }
                completion_details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
                safe_usage["completion_tokens_details"] = {
                    "reasoning_tokens": completion_details.get("reasoning_tokens")
                    if type(completion_details.get("reasoning_tokens")) is int and completion_details.get("reasoning_tokens") >= 0
                    else None
                }
                usage_record = {"page": page_result.page, "request_attempted": request_attempted, "usage": safe_usage}
                usage_pages.append(usage_record)
                page_reports.append({
                    "page": page_result.page,
                    "candidate_row_count": sum(
                        row["page"] == page_result.page
                        and row["server_role_candidate"] in {"numbered_position", "continuation_candidate"}
                        for row in candidates
                    ),
                    "model_kind_candidate_row_count": sum(
                        row["page"] == page_result.page
                        and row["model_kind"] in {"item", "component", "continuation", "unknown"}
                        for row in candidates
                    ),
                    "error_codes": [str(error).split(": ", 1)[-1] for error in page_result.errors],
                    "duration_ms": page_result.stats.get("elapsed_ms") if type(page_result.stats.get("elapsed_ms")) in (int, float) else None,
                    "request_attempted": request_attempted,
                    "usage": safe_usage,
                    "finish_reason": page_result.stats.get("finish_reason"),
                    "http_status": page_result.stats.get("http_status"),
                    "response_content_chars": page_result.stats.get("response_content_chars")
                    if type(page_result.stats.get("response_content_chars")) is int and page_result.stats.get("response_content_chars") >= 0
                    else None,
                    "response_content_bytes": page_result.stats.get("response_content_bytes")
                    if type(page_result.stats.get("response_content_bytes")) is int and page_result.stats.get("response_content_bytes") >= 0
                    else None,
                })
            preflight_by_page = {item["page"]: item for item in preflight_pages}
            report["pages"] = [
                {**preflight_by_page.get(item["page"], {}), **item}
                for item in page_reports
            ]
            report["errors"] = [error for page in page_reports for error in page["error_codes"]]
            report["duration_ms"] = result.stats.get("elapsed_ms")
            report["actual_usage"] = _aggregate_usage(usage_pages)
            report["cost_estimate"] = _cost_estimate(
                usage_pages, args.input_rub_per_1k, args.output_rub_per_1k,
                args.cached_input_rub_per_1k,
            )
            if reference is not None:
                comparison = _compare_reference(reference, candidates)
                _write_json_private(run_dir / "reference_comparison.json", comparison)
                report["reference_comparison"] = comparison["counts"]
            report["status"] = "completed_with_errors" if report["errors"] else "completed"
    except PilotError as exc:
        if exc.code not in report["errors"]:
            report["errors"].append(exc.code)
    except Exception:
        report["errors"].append("pilot_run_failed")
    finally:
        if report["duration_ms"] is None:
            report["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        try:
            _write_json_private(run_dir / "report.json", report)
        except PilotError:
            raise
    return (0 if report["status"] in {"dry_run_ready", "completed"} else 1), run_dir, report


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        exit_code, run_dir, report = run(args)
    except PilotError as exc:
        print(json.dumps({"status": "failed", "error_code": exc.code}, ensure_ascii=False))
        return 2
    print(json.dumps({
        "status": report["status"],
        "error_count": len(report["errors"]),
        "run_directory": str(run_dir),
        "report_file": str(run_dir / "report.json"),
    }, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
