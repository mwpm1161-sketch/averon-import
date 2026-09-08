"""Yandex Vision OCR provider (cloud profile).

Implements the provider-neutral OcrProvider contract on top of the official
Vision OCR async REST API (v1 routes):

    POST {vision_base_url}/ocr/v1/recognizeTextAsync    (submit, NOT idempotent)
    POST {vision_base_url}/ocr/v1/recognizeText         (single-page sync)
    GET  {vision_base_url}/ocr/v1/getRecognition?operationId=<id>

getRecognition answers with JSONL: one JSON object per PDF page, each of the
shape ``{"page": {...}, "textAnnotation": {...}}``. High-confidence raster
grids provide physical structure, while ``blocks/lines/words`` provide text.
Yandex tables remain structural evidence and the retained fallback. The API
key is resolved through SecretStore/env and is never persisted in caches,
results, logs or error messages.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Iterable

import fitz

from averon_import.services.ocr.base import (
    OcrProviderError,
    OcrResult,
    PageOcrResult,
)
from averon_import.services.ocr.page_contract import page_status_from_diagnostics
from averon_import.services.ocr.reconstruction import (
    collect_words,
    page_geometry,
    reconstruct_page_rows,
    target_cell_structural_safety,
)
from averon_import.services.ocr.critical_verification import (
    attach_exact_cell_candidate,
    attach_secondary_candidates,
)
from averon_import.services.ocr.physical_grid import (
    PhysicalGridDetection,
    validate_physical_grid,
)
from averon_import.services.ocr.raster_grid import (
    RasterGridPage,
    RasterRuledTableGridDetector,
    crop_has_glyph,
    crop_has_isolated_glyph,
    encode_png,
    physical_row_raster_witness,
    prepare_exact_cell_crop,
)
from averon_import.services.review_policy import CRITICAL_FIELDS, critical_field_count, is_critical_values
from averon_import.services.secrets import YANDEX_API_KEY, resolve_secret

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
SECONDARY_DPI = 600
SECONDARY_PADDING_RATIO = 0.01
MAX_PAGES_PER_REQUEST = 200
RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}
FAIL_FAST_STATUSES = {400, 401, 403}
CACHE_VERSION = 3
EXACT_CELL_FIELDS = ("quantity",)


def _submit_path() -> str:
    return "/ocr/v1/recognizeTextAsync"


def _sync_path() -> str:
    return "/ocr/v1/recognizeText"


def _recognition_path() -> str:
    return "/ocr/v1/getRecognition"


class _HttpFailure(Exception):
    def __init__(self, status: int, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = headers or {}


class _HttpResponse:
    def __init__(self, status: int, body: bytes, headers: dict | None = None):
        self.status = status
        self.body = body
        self.headers = headers or {}

    def json(self) -> dict:
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _HttpFailure(self.status, f"Некорректный JSON в ответе: {exc}") from exc
        return data if isinstance(data, dict) else {}


class UrllibHttpClient:
    """Minimal stdlib HTTP client; injectable fake in tests."""

    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        headers: dict | None = None,
        timeout: float = 30.0,
    ) -> _HttpResponse:
        request = urllib.request.Request(url, data=body, method=method)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return _HttpResponse(response.status, response.read(), dict(response.headers))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise _HttpFailure(exc.code, detail or str(exc.reason), dict(exc.headers)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise _HttpFailure(0, str(getattr(exc, "reason", exc))) from exc


class YandexVisionProvider:
    key = "yandex_vision"
    label = "Yandex Vision OCR"

    def __init__(
        self,
        settings_service,
        secret_store,
        cache_dir: Path | None = None,
        http: UrllibHttpClient | None = None,
        max_file_bytes: int = MAX_FILE_BYTES,
        max_pages_per_request: int = MAX_PAGES_PER_REQUEST,
        poll_interval_s: float = 2.0,
        poll_interval_max_s: float = 15.0,
        submit_attempts: int = 2,
        sleep_fn: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], float] = time.monotonic,
        grid_detector=None,
        reconstruction_mode: str = "geometry",
        env_keys: tuple[str, ...] = (
            "AVERON_YANDEX_VISION_API_KEY",
            "AVERON_YANDEX_AI_API_KEY",
        ),
    ) -> None:
        self._settings_service = settings_service
        self._secret_store = secret_store
        self._http = http or UrllibHttpClient()
        self._max_file_bytes = max_file_bytes
        self._max_pages_per_request = max_pages_per_request
        self._poll_interval_s = poll_interval_s
        self._poll_interval_max_s = poll_interval_max_s
        self._submit_attempts = submit_attempts
        self._sleep = sleep_fn
        self._now = now_fn
        self._grid_detector = grid_detector or RasterRuledTableGridDetector()
        self._reconstruction_mode = (
            reconstruction_mode
            if reconstruction_mode in {"table", "shadow", "geometry"}
            else "geometry"
        )
        self._env_keys = env_keys
        if cache_dir is not None:
            cache_dir = Path(cache_dir)
            (cache_dir / "results").mkdir(parents=True, exist_ok=True)
            (cache_dir / "pending").mkdir(parents=True, exist_ok=True)
        self._cache_dir = cache_dir

    # ------------------------------------------------------------------ config

    def _yandex_settings(self) -> dict:
        settings = getattr(self._settings_service.settings, "yandex", None)
        raw_languages = getattr(settings, "language_codes", None)
        if not isinstance(raw_languages, list):
            raw_languages = ["ru", "en"]
        language_codes = [str(code).strip() for code in raw_languages if str(code).strip()]
        return {
            "folder_id": str(getattr(settings, "folder_id", "") or "").strip(),
            "vision_model": str(getattr(settings, "vision_model", "table") or "table"),
            "vision_base_url": str(
                getattr(settings, "vision_base_url", "")
                or "https://ocr.api.cloud.yandex.net"
            ).rstrip("/"),
            "chunk_pages": int(getattr(settings, "chunk_pages", 8) or 8),
            "request_timeout_s": float(getattr(settings, "request_timeout_s", 120) or 120),
            "operation_timeout_s": float(
                getattr(settings, "operation_timeout_s", 600) or 600
            ),
            "language_codes": language_codes or ["ru", "en"],
        }

    def effective_api_key(self) -> str | None:
        for name in self._env_keys:
            raw = os.environ.get(name)
            if raw and raw.strip():
                return resolve_secret(raw, self._secret_store, YANDEX_API_KEY)
        return resolve_secret(None, self._secret_store, YANDEX_API_KEY)

    def available(self) -> bool:
        config = self._yandex_settings()
        return bool(config["folder_id"]) and self.effective_api_key() is not None

    def config_status(self) -> str:
        return "ready" if self.available() else "not_configured"

    def health(self) -> dict:
        config = self._yandex_settings()
        return {
            "provider": self.key,
            "available": self.available(),
            "folder_id_configured": bool(config["folder_id"]),
            "api_key_configured": self.effective_api_key() is not None,
            "vision_model": config["vision_model"],
            "vision_base_url": config["vision_base_url"],
            "chunk_pages": config["chunk_pages"],
            "language_codes": config["language_codes"],
            "reconstruction_mode": self._reconstruction_mode,
        }

    def _ensure_ready(self) -> dict:
        config = self._yandex_settings()
        if not config["folder_id"]:
            raise OcrProviderError(
                "Облачный OCR не настроен: не указан folder_id каталога "
                "Yandex Cloud (настройки приложения)."
            )
        if self.effective_api_key() is None:
            raise OcrProviderError(
                "Облачный OCR не настроен: отсутствует API ключ Yandex "
                "(настройки приложения)."
            )
        return config

    # ------------------------------------------------------------------- cache

    def _cache_key(
        self,
        source_digest: bytes,
        window: list[int],
        config: dict,
        strategy: str = "async",
    ) -> str:
        digest = hashlib.sha256()
        digest.update(b"yandex-vision-ocr\x00")
        digest.update(f"{CACHE_VERSION}\x00".encode())
        digest.update(f"{strategy}\x00".encode("utf-8"))
        digest.update(config["vision_base_url"].encode("utf-8"))
        digest.update(f"\x00{config['vision_model']}\x00".encode("utf-8"))
        digest.update(config["folder_id"].encode("utf-8"))
        digest.update(b"\x00")
        language_codes = config.get("language_codes") or []
        digest.update(f"{','.join(map(str, language_codes))}\x00".encode("utf-8"))
        digest.update(source_digest)
        digest.update(f"\x00{','.join(map(str, window))}".encode("utf-8"))
        return digest.hexdigest()

    def _cache_load(self, key: str, warnings: list[str] | None = None) -> dict | None:
        path = self._cache_dir / "results" / f"{key}.json" if self._cache_dir else None
        if not path or not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(data, dict)
                or data.get("provider") != self.key
                or data.get("version") != CACHE_VERSION
            ):
                raise ValueError("чужой, устаревший или некорректный формат кэша")
            return data
        except (OSError, ValueError, json.JSONDecodeError):
            if warnings is not None:
                warnings.append(
                    f"[{self.key}] повреждённый файл кэша OCR проигнорирован."
                )
            self._drop_quietly(path)
            return None

    def _cache_save(
        self, key: str, pages_payload: list, *, model: str | None = None
    ) -> None:
        if not self._cache_dir:
            return
        payload = {
            "version": CACHE_VERSION,
            "provider": self.key,
            "model": model or self._yandex_settings()["vision_model"],
            "cache_key": key,
            "pages": pages_payload,
        }
        path = self._cache_dir / "results" / f"{key}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _drop_quietly(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------- pending operations

    def _pending_path(self, key: str) -> Path | None:
        return self._cache_dir / "pending" / f"{key}.json" if self._cache_dir else None

    def _pending_load(self, key: str) -> dict | None:
        path = self._pending_path(key)
        if not path or not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("operation_id"):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        self._drop_quietly(path)
        return None

    def _pending_save(
        self, key: str, operation_id: str, page_numbers: list[int], recognition_base: str
    ) -> None:
        path = self._pending_path(key)
        if not path:
            return
        payload = {
            "operation_id": operation_id,
            "recognition_base": recognition_base,
            "pages": list(page_numbers),
            "created_at": time.time(),
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)

    def _pending_delete(self, key: str) -> None:
        path = self._pending_path(key)
        if path:
            self._drop_quietly(path)

    # -------------------------------------------------------------------- http

    @staticmethod
    def _auth_headers(api_key: str, folder_id: str) -> dict:
        headers = {
            "Authorization": f"Api-Key {api_key}",
            "Content-Type": "application/json",
        }
        if folder_id:
            headers["x-folder-id"] = folder_id
        return headers

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None,
        api_key: str,
        folder_id: str,
        timeout: float,
        cancel: threading.Event | None,
        warnings: list[str],
        allow_submit_retry: bool = False,
        allow_not_ready_404: bool = False,
        timeout_error_message: str | None = None,
    ) -> _HttpResponse:
        attempt = 0
        while True:
            self._check_cancel(cancel)
            attempt += 1
            try:
                response = self._http.request(
                    method,
                    url,
                    body=body,
                    headers=self._auth_headers(api_key, folder_id),
                    timeout=timeout,
                )
                if response.status < 400:
                    return response
                failure = self._failure_from_response(response)
            except _HttpFailure as exc:
                failure = exc
            if failure.status in FAIL_FAST_STATUSES:
                raise self._config_error(failure.status) from None
            if (
                allow_not_ready_404
                and failure.status == 404
                and self._is_not_ready_404(failure.message)
            ):
                raise failure
            retryable = failure.status in RETRYABLE_STATUSES or failure.status == 0
            limit = self._submit_attempts if allow_submit_retry else None
            if not (retryable and (limit is None or attempt < limit)):
                if failure.status == 0:
                    if timeout_error_message:
                        raise OcrProviderError(timeout_error_message) from None
                    raise OcrProviderError(
                        "Нет связи с Yandex Vision после повторов. Если запрос "
                        "всё же был принят сервисом, операция продолжится по "
                        "сохранённому идентификатору при следующем запуске."
                    ) from None
                raise OcrProviderError(
                    f"Ошибка Yandex Vision (HTTP {failure.status}): "
                    f"{failure.message[:300]}"
                ) from None
            if failure.status == 0:
                warnings.append(
                    f"[{self.key}] сеть/таймаут при обращении к Yandex "
                    f"(попытка {attempt}), повтор..."
                )
            retry_after = self._retry_after_seconds(failure.headers)
            self._sleep(retry_after if retry_after is not None else min(2 ** attempt, 8))

    @staticmethod
    def _failure_from_response(response: _HttpResponse) -> _HttpFailure:
        message = ""
        try:
            payload = json.loads(response.body.decode("utf-8"))
            if isinstance(payload, dict):
                message = str(payload.get("message") or payload.get("error") or "")
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        return _HttpFailure(
            response.status,
            message or f"HTTP {response.status}",
            dict(response.headers),
        )

    @staticmethod
    def _retry_after_seconds(headers: dict) -> float | None:
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_not_ready_404(message: str) -> bool:
        return "operation data is not ready" in str(message).lower()

    @staticmethod
    def _config_error(status: int) -> OcrProviderError:
        if status == 401:
            return OcrProviderError(
                "Yandex Vision отклонил ключ авторизации (401): проверьте "
                "API ключ в настройках приложения."
            )
        if status == 403:
            return OcrProviderError(
                "Доступ к Yandex Vision запрещён (403): проверьте права "
                "сервисного аккаунта и folder_id."
            )
        return OcrProviderError(
            f"Некорректный запрос к Yandex Vision ({status}): проверьте "
            "параметры облачного режима."
        )

    def _check_cancel(self, cancel: threading.Event | None) -> None:
        if cancel is not None and cancel.is_set():
            raise OcrProviderError("Распознавание отменено пользователем.")

    # -------------------------------------------------------------- chunk plan

    @staticmethod
    def _extract_subset_pdf(pdf_path: Path, page_numbers: Iterable[int]) -> bytes:
        with fitz.open(pdf_path) as source:
            with fitz.open() as target:
                for number in page_numbers:
                    target.insert_pdf(source, from_page=number - 1, to_page=number - 1)
                return target.tobytes()

    def _plan_chunks(
        self,
        pdf_path: Path,
        pages: list[int],
        *,
        one_page_per_request: bool = False,
    ) -> list[tuple[list[int], bytes]]:
        configured_target = max(
            1,
            min(
                int(self._yandex_settings()["chunk_pages"]),
                self._max_pages_per_request,
            ),
        )
        # Production Yandex processing is deliberately page-oriented while
        # the multi-page async implementation remains available to explicit
        # internal callers for future investigation.
        target = 1 if one_page_per_request else configured_target
        chunks: list[tuple[list[int], bytes]] = []
        index = 0
        while index < len(pages):
            size = min(target, len(pages) - index)
            window = pages[index : index + size]
            data = self._extract_subset_pdf(pdf_path, window)
            while size > 1 and len(data) > self._max_file_bytes:
                size = max(1, size // 2)
                window = pages[index : index + size]
                data = self._extract_subset_pdf(pdf_path, window)
            if len(data) > self._max_file_bytes:
                raise OcrProviderError(
                    f"Страница {window[0]} превышает лимит Yandex Vision "
                    f"({self._max_file_bytes // (1024 * 1024)} МБ) даже "
                    "по одной странице за запрос."
                )
            chunks.append((window, data))
            index += size
        return chunks

    # ------------------------------------------------------------ submit / poll

    def _submit(
        self,
        content: bytes,
        config: dict,
        api_key: str,
        cancel: threading.Event | None,
        warnings: list[str],
    ) -> str:
        body = json.dumps(
            {
                "folderId": config["folder_id"],
                "mimeType": "application/pdf",
                "model": config["vision_model"],
                "languageCodes": list(config["language_codes"]),
                "content": base64.b64encode(content).decode("ascii"),
            }
        ).encode("utf-8")
        response = self._request_with_retry(
            "POST",
            f"{config['vision_base_url']}{_submit_path()}",
            body=body,
            api_key=api_key,
            folder_id=config["folder_id"],
            timeout=config["request_timeout_s"],
            cancel=cancel,
            warnings=warnings,
            allow_submit_retry=True,
        )
        payload = response.json()
        operation = payload.get("operation") if isinstance(payload.get("operation"), dict) else payload
        operation_id = str(operation.get("id") or "").strip()
        if not operation_id:
            raise OcrProviderError(
                "Yandex Vision не вернул идентификатор операции; ответ: "
                f"{json.dumps(payload)[:200]}"
            )
        return operation_id

    def _recognize_sync(
        self,
        content: bytes,
        config: dict,
        api_key: str,
        cancel: threading.Event | None,
        warnings: list[str],
        progress: Callable[[str], None] | None = None,
        mime_type: str = "application/pdf",
    ) -> list[dict]:
        if progress:
            progress("Yandex OCR: отправляем страницу")
        body = json.dumps(
            {
                "folderId": config["folder_id"],
                "mimeType": mime_type,
                "model": config["vision_model"],
                "languageCodes": list(config["language_codes"]),
                "content": base64.b64encode(content).decode("ascii"),
            }
        ).encode("utf-8")
        if progress:
            progress("Yandex OCR: распознаём страницу")
        response = self._request_with_retry(
            "POST",
            f"{config['vision_base_url']}{_sync_path()}",
            body=body,
            api_key=api_key,
            folder_id=config["folder_id"],
            timeout=config["request_timeout_s"],
            cancel=cancel,
            warnings=warnings,
            allow_submit_retry=True,
            timeout_error_message=(
                "Синхронный OCR Yandex Vision превысил timeout "
                f"({config['request_timeout_s']} c)."
            ),
        )
        if not response.body.strip():
            raise OcrProviderError(
                "Синхронный OCR Yandex Vision вернул пустой ответ."
            )
        try:
            payload = response.json()
        except _HttpFailure as exc:
            raise OcrProviderError(
                f"Синхронный OCR Yandex Vision вернул некорректный JSON: "
                f"{exc.message[:300]}"
            ) from None
        result = payload.get("result")
        if not isinstance(result, dict) or not isinstance(
            result.get("textAnnotation"), dict
        ):
            raise OcrProviderError(
                "Ответ синхронного OCR Yandex Vision не содержит "
                "result.textAnnotation."
            )
        if progress:
            progress("Yandex OCR: результат получен")
        return [result]

    @staticmethod
    def _bbox_from_vertices(box: dict | None) -> tuple[float, float, float, float] | None:
        vertices = (box or {}).get("vertices") if isinstance(box, dict) else None
        if not isinstance(vertices, list):
            return None
        points: list[tuple[float, float]] = []
        for vertex in vertices:
            if not isinstance(vertex, dict):
                continue
            try:
                points.append((float(vertex.get("x", 0)), float(vertex.get("y", 0))))
            except (TypeError, ValueError):
                continue
        if not points:
            return None
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        return min(xs), min(ys), max(xs), max(ys)

    def _render_secondary_table_crop(
        self, pdf_path: Path, page_number: int, primary_payload: dict
    ) -> tuple[bytes, dict]:
        annotation = primary_payload.get("textAnnotation") or {}
        try:
            page_width = float(annotation.get("width") or 0)
            page_height = float(annotation.get("height") or 0)
        except (TypeError, ValueError):
            page_width = page_height = 0.0
        tables = [table for table in annotation.get("tables") or [] if isinstance(table, dict)]
        table = max(tables, key=lambda item: len(item.get("cells") or []), default=None)
        table_box = self._bbox_from_vertices((table or {}).get("boundingBox"))
        if page_width <= 0 or page_height <= 0 or table_box is None:
            raise OcrProviderError(
                "Вторичная проверка Yandex Vision невозможна: отсутствует "
                "геометрия primary table."
            )
        pad_x = (table_box[2] - table_box[0]) * SECONDARY_PADDING_RATIO
        pad_y = (table_box[3] - table_box[1]) * SECONDARY_PADDING_RATIO
        crop = {
            "x": max(0.0, (table_box[0] - pad_x) / page_width),
            "y": max(0.0, (table_box[1] - pad_y) / page_height),
            "width": min(1.0, (table_box[2] + pad_x) / page_width)
            - max(0.0, (table_box[0] - pad_x) / page_width),
            "height": min(1.0, (table_box[3] + pad_y) / page_height)
            - max(0.0, (table_box[1] - pad_y) / page_height),
        }
        with fitz.open(pdf_path) as document:
            page = document[page_number - 1]
            clip = fitz.Rect(
                page.rect.x0 + crop["x"] * page.rect.width,
                page.rect.y0 + crop["y"] * page.rect.height,
                page.rect.x0 + (crop["x"] + crop["width"]) * page.rect.width,
                page.rect.y0 + (crop["y"] + crop["height"]) * page.rect.height,
            )
            dpi = float(SECONDARY_DPI)
            while dpi >= 72:
                scale = dpi / 72.0
                pixels = clip.width * scale * clip.height * scale
                if pixels > MAX_IMAGE_PIXELS:
                    dpi *= math.sqrt(MAX_IMAGE_PIXELS / pixels) * 0.995
                    continue
                pixmap = page.get_pixmap(
                    matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False
                )
                content = pixmap.tobytes("png")
                if len(content) <= self._max_file_bytes:
                    return content, crop
                dpi *= 0.9
        raise OcrProviderError(
            "Область таблицы не удалось подготовить в лимитах Yandex Vision "
            f"({self._max_file_bytes // (1024 * 1024)} МБ / {MAX_IMAGE_PIXELS} MP)."
        )

    def _secondary_verify_page(
        self,
        pdf_path: Path,
        page_number: int,
        primary_payload: dict,
        primary_rows: list,
        source_digest: bytes,
        config: dict,
        api_key: str,
        cancel: threading.Event | None,
        warnings: list[str],
        stats: dict[str, int],
        progress: Callable[[str], None] | None = None,
    ) -> tuple[dict, dict] | None:
        missing = any(
            is_critical_values(row.values)
            and any(
                not str(row.values.get(field, "") or "").strip()
                and isinstance((row.metadata.get("cell_bboxes") or {}).get(field), dict)
                for field in CRITICAL_FIELDS
            )
            for row in primary_rows
        )
        if not missing:
            return None
        try:
            content, crop = self._render_secondary_table_crop(
                pdf_path, page_number, primary_payload
            )
            secondary_config = {**config, "vision_model": "table"}
            key = self._cache_key(
                source_digest, [page_number], secondary_config, strategy="secondary-table"
            )
            cached = self._cache_load(key, warnings)
            if cached is not None:
                pages_payload = cached.get("pages") or []
            else:
                stats["secondary_requests"] += 1
                if progress:
                    progress(
                        "Yandex OCR: проверяем критичные поля вторым проходом"
                    )
                pages_payload = self._recognize_sync(
                    content,
                    secondary_config,
                    api_key,
                    cancel,
                    warnings,
                    progress=None,
                    mime_type="image/png",
                )
                self._cache_save(key, pages_payload, model="table")
            if not pages_payload:
                return None
            return pages_payload[0], crop
        except (OcrProviderError, OSError, ValueError) as exc:
            warnings.append(
                f"[{self.key}] вторичная проверка critical-полей не выполнена: "
                f"{str(exc)[:300]}"
            )
            return None

    def _detect_grid_page(
        self,
        pdf_path: Path,
        page_number: int,
        payload: dict,
        warnings: list[str],
    ) -> tuple[PhysicalGridDetection | None, RasterGridPage | None]:
        if self._reconstruction_mode == "table":
            return None, None
        annotation = payload.get("textAnnotation")
        if not isinstance(annotation, dict) or not collect_words(annotation):
            return None, None
        try:
            analyze_page = getattr(self._grid_detector, "analyze_page", None)
            if callable(analyze_page):
                raster = analyze_page(pdf_path, page_number)
                return raster.detection, raster
            detection = self._grid_detector.detect_page(pdf_path, page_number)
            return detection, None
        except (OSError, ValueError, RuntimeError) as exc:
            warnings.append(
                f"[{self.key}] CV grid недоступен, используется table fallback: "
                f"{str(exc)[:240]}"
            )
            return None, None

    @staticmethod
    def _isolated_page_text(payload: dict) -> str:
        annotation = payload.get("textAnnotation") if isinstance(payload, dict) else None
        if not isinstance(annotation, dict):
            return ""
        full_text = str(annotation.get("fullText") or "").strip()
        if full_text:
            return full_text
        return " ".join(
            str(word.get("text") or "").strip()
            for word in collect_words(annotation)
            if str(word.get("text") or "").strip()
        )

    def _exact_cell_verify_page(
        self,
        page_number: int,
        raster: RasterGridPage | None,
        rows: list,
        config: dict,
        api_key: str,
        cancel: threading.Event | None,
        warnings: list[str],
        stats: dict[str, int],
        progress: Callable[[str], None] | None = None,
    ) -> None:
        if (
            self._reconstruction_mode != "geometry"
            or raster is None
            or not raster.detection.high_confidence
            or raster.detection.grid is None
        ):
            return
        grid = raster.detection.grid
        if validate_physical_grid(grid):
            return
        actual_requests = 0
        for row in rows:
            if not is_critical_values(row.values):
                continue
            row_metadata = row.metadata if isinstance(row.metadata, dict) else {}
            if set(row_metadata.get("review_reasons") or {}).intersection(
                {
                    "ambiguous_table_schema",
                    "structural_schema_ambiguous",
                    "unsupported_table_schema",
                    "schema_unknown",
                }
            ):
                continue
            refs = row.metadata.get("physical_grid_cells") or {}
            for field in EXACT_CELL_FIELDS:
                if str(row.values.get(field, "") or "").strip():
                    continue
                ref = refs.get(field)
                if not isinstance(ref, dict):
                    continue
                try:
                    grid_row = int(ref["row_index"])
                    grid_column = int(ref["column_index"])
                except (KeyError, TypeError, ValueError):
                    continue
                cell = grid.cell(grid_row, grid_column)
                if cell is None:
                    continue
                local_safety = target_cell_structural_safety(row, field, grid)
                row_metadata.setdefault("target_cell_structural_safety", {})[
                    field
                ] = local_safety
                if not local_safety["safe"]:
                    continue
                crop = prepare_exact_cell_crop(raster, cell, scale=2)
                if not crop_has_glyph(crop):
                    continue
                stats["exact_cell_checked"] += 1
                try:
                    content = encode_png(crop)
                    if len(content) > self._max_file_bytes:
                        raise OcrProviderError(
                            "Exact-cell PNG превышает лимит Yandex Vision."
                        )
                    cell_config = {**config, "vision_model": "page"}
                    content_digest = hashlib.sha256(content).digest()
                    key = self._cache_key(
                        content_digest,
                        [page_number],
                        cell_config,
                        strategy="critical-cell-2x-v1",
                    )
                    cached = self._cache_load(key, warnings)
                    if cached is not None:
                        pages_payload = cached.get("pages") or []
                    else:
                        if actual_requests:
                            self._check_cancel(cancel)
                            self._sleep(2.0)
                        actual_requests += 1
                        stats["exact_cell_requests"] += 1
                        if progress:
                            progress(
                                "Yandex OCR: проверяем точную критичную ячейку"
                            )
                        pages_payload = self._recognize_sync(
                            content,
                            cell_config,
                            api_key,
                            cancel,
                            warnings,
                            progress=None,
                            mime_type="image/png",
                        )
                        self._cache_save(key, pages_payload, model="page")
                    if not pages_payload:
                        continue
                    raw_value = self._isolated_page_text(pages_payload[0])
                    if attach_exact_cell_candidate(
                        row,
                        field,
                        raw_value,
                        bbox=cell.as_bbox(),
                    ):
                        stats["exact_cell_candidates"] += 1
                except OcrProviderError as exc:
                    # Cancellation is a job-level decision, not a recoverable
                    # failure of one optional verification candidate.
                    if cancel is not None and cancel.is_set():
                        raise
                    warnings.append(
                        f"[{self.key}] exact-cell проверка {field} не выполнена: "
                        f"{str(exc)[:240]}"
                    )
                except (OSError, ValueError) as exc:
                    warnings.append(
                        f"[{self.key}] exact-cell проверка {field} не выполнена: "
                        f"{str(exc)[:240]}"
                    )

    @staticmethod
    def _position_mapping_present(metadata: dict) -> bool:
        mapping = metadata.get("column_mapping") or {}
        return any(
            "position" in (
                {str(keys)}
                if isinstance(keys, str)
                else {str(key) for key in (keys or [])}
            )
            for keys in mapping.values()
        )

    def _mark_identity_cell_loss(
        self,
        raster: RasterGridPage | None,
        rows: list,
    ) -> int:
        """Mark independently witnessed missing position OCR without guessing it."""
        if (
            raster is None
            or not raster.detection.high_confidence
            or raster.detection.grid is None
            or validate_physical_grid(raster.detection.grid)
        ):
            return 0
        grid = raster.detection.grid
        marked = 0
        for row in rows:
            metadata = row.metadata if isinstance(row.metadata, dict) else {}
            schema = metadata.get("schema_assessment")
            if (
                not isinstance(schema, dict)
                or str(schema.get("status") or "").lower() != "supported"
                or not metadata.get("structured_table")
                or not metadata.get("provider_has_explicit_rows")
                or not self._position_mapping_present(metadata)
                or str(row.values.get("position", "") or "").strip()
                or not is_critical_values(row.values)
            ):
                continue
            local_safety = target_cell_structural_safety(row, "position", grid)
            identity_safety_reasons = [
                reason
                for reason in local_safety["reasons"]
                if reason != "unlocalized_structural_ambiguity"
            ]
            if identity_safety_reasons:
                continue
            ref = (metadata.get("physical_grid_cells") or {}).get("position")
            if not isinstance(ref, dict):
                continue
            cell = grid.cell(local_safety["row_index"], local_safety["column_index"])
            if cell is None or not crop_has_isolated_glyph(
                prepare_exact_cell_crop(raster, cell, scale=2)
            ):
                continue
            metadata.update({
                "identity_cell_missing": True,
                "identity_field": "position",
                "identity_cell_bbox": cell.as_bbox(),
                "identity_cell_row": cell.row_index,
                "identity_cell_column": cell.column_index,
                "identity_raster_glyph": True,
                "identity_candidate_source": None,
            })
            reasons = list(metadata.get("review_reasons") or [])
            if "identity_cell_missing" not in reasons:
                reasons.append("identity_cell_missing")
            metadata["review_reasons"] = reasons
            marked += 1
        return marked

    def _recognition_url(self, operation_id: str, recognition_base: str) -> str:
        return f"{recognition_base}{_recognition_path()}?operationId={operation_id}"

    def _poll_once(
        self, operation_id: str, recognition_base: str, config: dict, api_key: str, cancel
    ) -> _HttpResponse | None:
        warnings: list[str] = []
        try:
            return self._request_with_retry(
                "GET",
                self._recognition_url(operation_id, recognition_base),
                body=None,
                api_key=api_key,
                folder_id=config["folder_id"],
                timeout=config["request_timeout_s"],
                cancel=cancel,
                warnings=warnings,
                allow_not_ready_404=True,
            )
        except _HttpFailure as exc:
            if exc.status == 404 and self._is_not_ready_404(exc.message):
                return None
            raise

    def _parse_jsonl(self, body: bytes) -> tuple[dict | None, list[dict]]:
        """Split a getRecognition JSONL answer into status line + page payloads.

        Empty lines are ignored; each non-empty line must decode to a JSON
        object. Returns ``(status_payload, pages)`` where ``status_payload`` is
        the first line when it carries operation state instead of a page.
        """
        text = body.decode("utf-8", errors="replace")
        objects: list[dict] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise OcrProviderError(
                    f"Ответ Yandex Vision не является корректным JSONL: {exc}"
                ) from None
            if not isinstance(parsed, dict):
                raise OcrProviderError(
                    "Ответ Yandex Vision содержит строку без JSON-объекта."
                )
            objects.append(parsed)
        if not objects:
            return None, []
        first = objects[0]
        looks_like_page = isinstance(first.get("textAnnotation"), dict)
        if looks_like_page:
            return None, objects
        return first, objects[1:]

    @staticmethod
    def _page_payloads(objects: list[dict]) -> list[dict]:
        pages: list[dict] = []
        for payload in objects:
            if isinstance(payload.get("textAnnotation"), dict):
                pages.append(payload)
        return pages

    def _poll_recognition(
        self,
        operation_id: str,
        recognition_base: str,
        config: dict,
        api_key: str,
        cancel: threading.Event | None,
        progress: Callable[[str], None] | None = None,
    ) -> list[dict]:
        started = self._now()
        deadline = started + config["operation_timeout_s"]
        interval = self._poll_interval_s
        while True:
            self._check_cancel(cancel)
            response = self._poll_once(
                operation_id, recognition_base, config, api_key, cancel
            )
            if response is not None and response.body.strip():
                status, objects = self._parse_jsonl(response.body)
                if status is None:
                    return self._page_payloads(objects)
                state = str(status.get("state") or "").lower() or (
                    "done" if status.get("done") else ""
                )
                if state == "error" or isinstance(status.get("error"), dict):
                    error = status.get("error") or {}
                    message = str(error.get("message", "неизвестная ошибка"))
                    raise OcrProviderError(
                        f"Операция Yandex Vision завершилась ошибкой: {message[:300]}"
                    )
                if state == "done":
                    return self._page_payloads(objects)
                if state not in ("", "pending", "running"):
                    raise OcrProviderError(
                        f"Неизвестное состояние операции Yandex Vision: {state[:100]}"
                    )
            if self._now() > deadline:
                raise OcrProviderError(
                    f"Превышено время ожидания операции Yandex Vision "
                    f"({int(config['operation_timeout_s'])} c). Идентификатор "
                    "операции сохранён — при следующем запуске опрос будет "
                    "продолжен без повторной отправки документа."
                )
            if progress:
                elapsed = max(0, int(self._now() - started))
                progress(f"Yandex OCR: ожидаем результат, {elapsed} с")
            self._sleep(interval)
            interval = min(interval * 2, self._poll_interval_max_s)
            continue

    # ---------------------------------------------------------------- pipeline

    def recognize(
        self,
        pdf_path: Path,
        pages=None,
        *,
        pages_dir=None,
        dpi=None,
        crop=None,
        mode=None,
        progress: Callable[..., None] | None = None,
        cancel: threading.Event | None = None,
    ) -> OcrResult:
        config = self._ensure_ready()
        api_key = self.effective_api_key() or ""
        requested = sorted({int(page) for page in (pages or [])})
        if not requested:
            raise OcrProviderError("Не выбраны страницы для облачного распознавания.")
        on_progress = progress or (lambda *args, **kwargs: None)
        source_digest = hashlib.sha256(Path(pdf_path).read_bytes()).digest()
        planned = self._plan_chunks(
            pdf_path,
            requested,
            one_page_per_request=True,
        )
        total_pages = len(requested)
        stats = {
            "primary_requests": 0,
            "secondary_requests": 0,
            "secondary_candidates": 0,
            "secondary_recovered": 0,
            "geometry_high_confidence_pages": 0,
            "geometry_selected_pages": 0,
            "geometry_fallback_pages": 0,
            "geometry_shadow_pages": 0,
            "geometry_structural_disagreements": 0,
            "geometry_shadow_field_differences": 0,
            "exact_cell_checked": 0,
            "exact_cell_requests": 0,
            "exact_cell_candidates": 0,
            "identity_cell_missing": 0,
            "unresolved_critical": 0,
        }
        by_page: dict[int, PageOcrResult] = {
            number: PageOcrResult(page=number, provides_confidence=False)
            for number in requested
        }

        for index, (window, content) in enumerate(planned):
            self._check_cancel(cancel)
            page_number = window[0]
            on_progress(
                index,
                total_pages,
                f"Yandex OCR: страница {index + 1} из {total_pages} — {page_number}",
            )
            warnings: list[str] = []
            parsed = self._recognize_chunk(
                window,
                content,
                source_digest,
                config,
                api_key,
                cancel,
                warnings,
                progress=lambda message, page_index=index: on_progress(
                    page_index, total_pages, message
                ),
                stats=stats,
            )
            mapped = self._map_pages_to_numbers(parsed, window, warnings)
            for message in warnings:
                for number in window:
                    target = by_page.setdefault(
                        number,
                        PageOcrResult(page=number, provides_confidence=False),
                    )
                    if message not in target.errors:
                        target.errors.append(message)
            for number, payload in mapped:
                target = by_page.setdefault(
                    number,
                    PageOcrResult(page=number, provides_confidence=False),
                )
                grid_detection, raster = self._detect_grid_page(
                    pdf_path, number, payload, warnings
                )
                physical_grid = (
                    grid_detection.grid if grid_detection is not None else None
                )
                if grid_detection is not None and grid_detection.high_confidence:
                    stats["geometry_high_confidence_pages"] += 1
                reconstruction_diagnostics: dict = {}
                rows = reconstruct_page_rows(
                    payload,
                    self.key,
                    physical_grid=physical_grid,
                    reconstruction_mode=self._reconstruction_mode,
                    diagnostics=reconstruction_diagnostics,
                )
                if grid_detection is not None and physical_grid is None:
                    reconstruction_diagnostics["geometry_source"] = (
                        grid_detection.source
                    )
                    reconstruction_diagnostics["grid_confidence"] = 0.0
                    reconstruction_diagnostics["candidate_count"] = (
                        grid_detection.candidate_count
                    )
                    reconstruction_diagnostics["selected_candidate"] = (
                        grid_detection.selected_candidate
                    )
                    if grid_detection.reasons:
                        reconstruction_diagnostics["fallback_reason"] = (
                            grid_detection.reasons[0]
                        )
                    reconstruction_diagnostics["detector_reasons"] = list(
                        grid_detection.reasons
                    )
                secondary = self._secondary_verify_page(
                    pdf_path,
                    number,
                    payload,
                    rows,
                    source_digest,
                    config,
                    api_key,
                    cancel,
                    warnings,
                    stats,
                    progress=lambda message, page_index=index: on_progress(
                        page_index, total_pages, message
                    ),
                )
                if secondary is not None:
                    secondary_payload, secondary_crop = secondary
                    rows = reconstruct_page_rows(
                        payload,
                        self.key,
                        secondary_payload=secondary_payload,
                        secondary_crop=secondary_crop,
                        physical_grid=physical_grid,
                        reconstruction_mode=self._reconstruction_mode,
                        diagnostics=reconstruction_diagnostics,
                    )
                    secondary_result = attach_secondary_candidates(
                        rows,
                        secondary_payload,
                        crop=secondary_crop,
                    )
                    stats["secondary_candidates"] += secondary_result[
                        "secondary_candidates"
                    ]
                    stats["secondary_recovered"] += secondary_result[
                        "secondary_recovered"
                    ]
                if (
                    raster is not None
                    and physical_grid is not None
                    and reconstruction_diagnostics.get("selected_mode") == "geometry_first"
                ):
                    covered_rows = {
                        int(row.metadata.get("source_row_index"))
                        for row in rows
                        if isinstance(row.metadata, dict)
                        and str(row.metadata.get("source_row_index", "")).lstrip("-").isdigit()
                    }
                    witness = physical_row_raster_witness(
                        raster,
                        physical_grid,
                        reconstruction_diagnostics.get("physical_body_row_indexes"),
                        covered_rows,
                    )
                    reconstruction_diagnostics["physical_row_coverage"] = witness
                    if witness.get("suspected_loss_rows"):
                        reconstruction_diagnostics["physical_row_loss_suspected"] = True
                        reconstruction_diagnostics["physical_row_loss_rows"] = list(
                            witness["suspected_loss_rows"]
                        )
                identity_missing = self._mark_identity_cell_loss(raster, rows)
                stats["identity_cell_missing"] += identity_missing
                reconstruction_diagnostics["identity_cell_missing_count"] = (
                    identity_missing
                )
                self._exact_cell_verify_page(
                    number,
                    raster,
                    rows,
                    config,
                    api_key,
                    cancel,
                    warnings,
                    stats,
                    progress=lambda message, page_index=index: on_progress(
                        page_index, total_pages, message
                    ),
                )
                target.page_status = page_status_from_diagnostics(
                    number,
                    reconstruction_diagnostics,
                    row_count=len(rows),
                ).as_dict()
                selected_mode = reconstruction_diagnostics.get("selected_mode")
                if selected_mode == "geometry_first":
                    stats["geometry_selected_pages"] += 1
                elif selected_mode == "table_shadow":
                    stats["geometry_shadow_pages"] += 1
                elif self._reconstruction_mode != "table":
                    stats["geometry_fallback_pages"] += 1
                structural = reconstruction_diagnostics.get("structural_evidence") or {}
                if structural.get("has_disagreement"):
                    stats["geometry_structural_disagreements"] += 1
                shadow_diff = reconstruction_diagnostics.get("shadow_diff") or {}
                stats["geometry_shadow_field_differences"] += int(
                    shadow_diff.get("field_difference_count") or 0
                )
                for message in warnings:
                    if message not in target.errors:
                        target.errors.append(message)
                target.rows.extend(rows)
                geometry = page_geometry(payload)
                if grid_detection is not None:
                    geometry["physical_grid"] = grid_detection.as_dict()
                if reconstruction_diagnostics:
                    geometry["reconstruction"] = reconstruction_diagnostics
                if geometry:
                    target.geometry = geometry
            on_progress(
                index + 1,
                total_pages,
                f"Yandex OCR: страница {index + 1} из {total_pages} — "
                f"{page_number} готова",
            )
        stats["unresolved_critical"] = sum(
            critical_field_count(row)
            for page in by_page.values()
            for row in [
                {
                    "row_type": "item",
                    "name": raw.values.get("name", ""),
                    "quantity": raw.values.get("quantity", ""),
                    "unit": raw.values.get("unit", ""),
                    "mass": raw.values.get("mass", ""),
                    "ocr_metadata": raw.metadata,
                    "review_reasons": raw.metadata.get("review_reasons", []),
                }
                for raw in page.rows
            ]
        )
        return OcrResult(
            provider=self.key,
            pages=[by_page[number] for number in requested],
            stats=stats,
        )

    def _recognize_chunk(
        self,
        window: list[int],
        content: bytes,
        source_digest: bytes,
        config: dict,
        api_key: str,
        cancel: threading.Event | None,
        warnings: list[str],
        progress: Callable[[str], None] | None = None,
        stats: dict[str, int] | None = None,
    ) -> list:
        strategy = "sync" if len(window) == 1 else "async"
        key = self._cache_key(source_digest, window, config, strategy=strategy)
        cached = self._cache_load(key, warnings)
        if cached is not None:
            return cached.get("pages") or []
        if strategy == "sync":
            if stats is not None:
                stats["primary_requests"] += 1
            pages_payload = self._recognize_sync(
                content,
                config,
                api_key,
                cancel,
                warnings,
                progress=progress,
            )
            self._cache_save(key, pages_payload)
            return pages_payload
        pending = self._pending_load(key)
        operation_id = pending["operation_id"] if pending else None
        recognition_base = str(pending.get("recognition_base") or "").strip() if pending else ""
        if operation_id is None:
            if stats is not None:
                stats["primary_requests"] += 1
            operation_id = self._submit(content, config, api_key, cancel, warnings)
            recognition_base = config["vision_base_url"]
            self._pending_save(key, operation_id, window, recognition_base)
        pages_payload = self._poll_recognition(
            operation_id,
            recognition_base or config["vision_base_url"],
            config,
            api_key,
            cancel,
            progress=progress,
        )
        self._pending_delete(key)
        if not pages_payload:
            raise OcrProviderError(
                "Ответ Yandex Vision не содержит страниц с результатом OCR."
            )
        self._cache_save(key, pages_payload)
        return pages_payload

    def _map_pages_to_numbers(
        self, pages_payload: list, window: list[int], warnings: list[str]
    ) -> list[tuple[int, dict]]:
        if len(pages_payload) != len(window):
            warnings.append(
                f"[{self.key}] Yandex вернул {len(pages_payload)} страниц(ы) "
                f"для запрошенных {len(window)}; сопоставление по порядку."
            )
        mapped: list[tuple[int, dict]] = []
        for offset, payload in enumerate(pages_payload):
            number = window[offset] if offset < len(window) else window[-1]
            if isinstance(payload, dict):
                mapped.append((number, payload))
        return mapped
