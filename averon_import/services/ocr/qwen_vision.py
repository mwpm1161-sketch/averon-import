"""Experimental image-to-table OcrProvider; intentionally not registered anywhere.

V1 never produces USABLE, physical row refs, OCR confidence or verified data.
Raw extraction is held only in result metadata; diagnostic fields are content-free.
"""

from __future__ import annotations

import math
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Callable

import fitz
from PIL import Image

from averon_import.services.ocr.base import OcrResult, PageOcrResult, ProgressCallback
from averon_import.services.ocr.page_contract import OUTPUT_NO_SPEC, OUTPUT_REVIEW_REQUIRED, PageExtractionStatus
from averon_import.services.ocr.qwen_vision_client import (
    QwenVisionClient, QwenVisionConfig, QwenVisionError, VisionTransport, _check_cancel,
)
from averon_import.services.ocr.qwen_vision_contract import VisionPage, project_page
from averon_import.services.pdf_service import PdfService


def _crop_area(value: dict | None) -> dict[str, float] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"x", "y", "width", "height"}:
        raise QwenVisionError("invalid_crop")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in value.values()):
        raise QwenVisionError("invalid_crop")
    crop = {key: float(item) for key, item in value.items()}
    if (crop["x"] < 0 or crop["y"] < 0 or crop["width"] <= 0 or crop["height"] <= 0
            or crop["x"] + crop["width"] > 1 or crop["y"] + crop["height"] > 1):
        raise QwenVisionError("invalid_crop")
    return crop


class QwenVisionProvider:
    key = "qwen_vision"
    label = "Qwen Vision (experimental)"

    def __init__(self, config: QwenVisionConfig, *, pdf_service: PdfService | None = None,
                 transport: VisionTransport | None = None, clock: Callable[[], float] = time.monotonic):
        self.config = config
        self._pdf = pdf_service if pdf_service is not None else PdfService()
        self._clock = clock
        self._client = QwenVisionClient(config, transport=transport, clock=clock)

    def available(self) -> bool:
        return self.config.configured

    def health(self) -> dict:
        return {"available": self.available(), "provider": self.key, "experimental": True, "api_verified": False}

    def recognize(
        self, pdf_path: Path, pages: list[int], *, pages_dir: Path | None = None,
        dpi: int | None = None, crop: dict | None = None, mode: str | None = None,
        progress: ProgressCallback | None = None, cancel: threading.Event | None = None,
        transcription_callback: Callable[[int, VisionPage], None] | None = None,
    ) -> OcrResult:
        # pages_dir is intentionally unused: page images stay in memory and no
        # cached image from another PDF/crop is reused. mode does not select AI correction.
        limits = self.config.limits
        if not pages or any(type(page) is not int or page < 1 for page in pages):
            raise QwenVisionError("invalid_pages")
        selected = sorted(set(pages))
        if len(selected) > limits.max_pages:
            raise QwenVisionError("page_limit")
        render_dpi = limits.dpi if dpi is None else dpi
        if type(render_dpi) is not int or not 1 <= render_dpi <= limits.dpi:
            raise QwenVisionError("dpi_limit")
        image_crop = _crop_area(crop)
        if mode not in (None, "standard", "accurate"):
            raise QwenVisionError("unsupported_mode")
        if not self.available():
            raise QwenVisionError("not_configured")
        started = self._clock()
        deadline = started + limits.total_timeout_s
        output = OcrResult(provider=self.key, stats={"experimental": True, "api_verified": False, "requests": 0})
        for index, page in enumerate(selected):
            _check_cancel(cancel)
            if progress:
                progress(index, len(selected), f"Qwen Vision: подготовка страницы {page}")
            _check_cancel(cancel)
            target = PageOcrResult(page=page, provides_confidence=False)
            code = ""
            try:
                if output.stats["requests"] >= limits.max_requests:
                    raise QwenVisionError("request_budget_exhausted")
                if self._clock() >= deadline:
                    raise QwenVisionError("total_timeout")
                # PdfService renders the image. Inspect dimensions first so a
                # giant PDF page cannot allocate an unbounded pixmap at this DPI.
                with fitz.open(pdf_path) as document:
                    if page > document.page_count:
                        raise QwenVisionError("invalid_page")
                    rect = document[page - 1].rect
                    pixels = math.ceil(rect.width * render_dpi / 72) * math.ceil(rect.height * render_dpi / 72)
                    if pixels > limits.max_image_pixels:
                        raise QwenVisionError("image_pixel_limit")
                image = self._pdf.render_page(Path(pdf_path), page, dpi=render_dpi)
                _check_cancel(cancel)
                if image_crop:
                    # Validate original dimensions before allocating a cropped image.
                    if len(image) > limits.max_image_bytes:
                        raise QwenVisionError("image_size_limit")
                    with Image.open(BytesIO(image)) as opened:
                        if opened.width * opened.height > limits.max_image_pixels:
                            raise QwenVisionError("image_pixel_limit")
                        left = int(image_crop["x"] * opened.width)
                        top = int(image_crop["y"] * opened.height)
                        right = int((image_crop["x"] + image_crop["width"]) * opened.width)
                        bottom = int((image_crop["y"] + image_crop["height"]) * opened.height)
                        if right <= left or bottom <= top:
                            raise QwenVisionError("invalid_crop")
                        buffer = BytesIO()
                        opened.crop((left, top, right, bottom)).save(buffer, format="PNG")
                        image = buffer.getvalue()
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise QwenVisionError("total_timeout")
                completion = self._client.extract(image, page, timeout_s=remaining, cancel=cancel)
                if transcription_callback is not None:
                    transcription_callback(page, completion.page)
                target.stats = completion.diagnostics
                target.rows = project_page(completion.page, image_crop=image_crop)
                if not any(not row.metadata.get("vision_evidence_only") for row in target.rows):
                    code = "empty_extraction"
            except QwenVisionError as exc:
                if exc.code == "cancelled":
                    raise
                code = exc.code
                target.stats = exc.diagnostics
            except Exception:
                # PDF/Pillow exceptions may contain document text or filenames.
                code = "page_extraction_failed"
            _check_cancel(cancel)
            output.stats["requests"] += int(target.stats.get("request_attempted") is True)
            if code:
                target.errors.append(f"Qwen Vision: {code}")
            target.page_status = PageExtractionStatus(
                page=page,
                output_status=OUTPUT_REVIEW_REQUIRED if target.rows and not code else OUTPUT_NO_SPEC,
                blockers=["qwen_vision_experimental", "structural_layout_ambiguous"] + ([code] if code else []),
                diagnostics={"provider": self.key, "experimental": True, "local_geometry_verified": False,
                             "api_verified": False, "error_code": code or None},
            ).as_dict()
            output.pages.append(target)
            if progress:
                progress(index + 1, len(selected), f"Qwen Vision: страница {page} требует проверки")
        output.stats["elapsed_ms"] = round(max(0, self._clock() - started) * 1000, 3)
        output.stats["usage_complete_pages"] = sum(
            all(key in item.stats.get("usage", {}) for key in ("prompt_tokens", "completion_tokens", "total_tokens"))
            for item in output.pages
        )
        # Totals contain only reported counts. Completeness stays explicit; no
        # missing usage is invented as zero and no image-token estimate is billed.
        output.stats["reported_usage"] = {
            key: sum(item.stats["usage"][key] for item in output.pages if key in item.stats.get("usage", {}))
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if any(key in item.stats.get("usage", {}) for item in output.pages)
        }
        return output
