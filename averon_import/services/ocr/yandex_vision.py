"""Yandex Vision OCR provider (cloud profile).

Implements the provider-neutral OcrProvider contract on top of the official
Vision OCR async REST API (v1 routes):

    POST {vision_base_url}/ocr/v1/recognizeTextAsync    (submit, NOT idempotent)
    GET  {vision_base_url}/ocr/v1/getRecognition?operationId=<id>

getRecognition answers with JSONL: one JSON object per PDF page, each of the
shape ``{"page": {...}, "textAnnotation": {...}}``. Tables inside
``textAnnotation`` are the primary reconstruction source; blocks/lines/words
geometry is only a fallback (see ``reconstruction.py``). The API key is
resolved through SecretStore/env and is never persisted in caches, results,
logs or error messages.
"""

from __future__ import annotations

import base64
import hashlib
import json
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
from averon_import.services.ocr.reconstruction import (
    page_geometry,
    reconstruct_page_rows,
)
from averon_import.services.secrets import YANDEX_API_KEY, resolve_secret

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_PAGES_PER_REQUEST = 200
RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}
FAIL_FAST_STATUSES = {400, 401, 403}
CACHE_VERSION = 2


def _submit_path() -> str:
    return "/ocr/v1/recognizeTextAsync"


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

    def _cache_key(self, source_digest: bytes, window: list[int], config: dict) -> str:
        digest = hashlib.sha256()
        digest.update(b"yandex-vision-ocr\x00")
        digest.update(f"{CACHE_VERSION}\x00".encode())
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

    def _cache_save(self, key: str, pages_payload: list) -> None:
        if not self._cache_dir:
            return
        payload = {
            "version": CACHE_VERSION,
            "provider": self.key,
            "model": self._yandex_settings()["vision_model"],
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
            retryable = failure.status in RETRYABLE_STATUSES or failure.status == 0
            limit = self._submit_attempts if allow_submit_retry else None
            if not (retryable and (limit is None or attempt < limit)):
                if failure.status == 0:
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
        self, pdf_path: Path, pages: list[int]
    ) -> list[tuple[list[int], bytes]]:
        target = max(1, min(int(self._yandex_settings()["chunk_pages"]), self._max_pages_per_request))
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

    def _recognition_url(self, operation_id: str, recognition_base: str) -> str:
        return f"{recognition_base}{_recognition_path()}?operationId={operation_id}"

    def _poll_once(
        self, operation_id: str, recognition_base: str, config: dict, api_key: str, cancel
    ) -> _HttpResponse:
        warnings: list[str] = []
        return self._request_with_retry(
            "GET",
            self._recognition_url(operation_id, recognition_base),
            body=None,
            api_key=api_key,
            folder_id=config["folder_id"],
            timeout=config["request_timeout_s"],
            cancel=cancel,
            warnings=warnings,
        )

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
    ) -> list[dict]:
        deadline = self._now() + config["operation_timeout_s"]
        interval = self._poll_interval_s
        while True:
            self._check_cancel(cancel)
            response = self._poll_once(
                operation_id, recognition_base, config, api_key, cancel
            )
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
            if state in ("", "pending", "running"):
                if self._now() > deadline:
                    raise OcrProviderError(
                        f"Превышено время ожидания операции Yandex Vision "
                        f"({int(config['operation_timeout_s'])} c). Идентификатор "
                        "операции сохранён — при следующем запуске опрос будет "
                        "продолжен без повторной отправки документа."
                    )
                self._sleep(interval)
                interval = min(interval * 2, self._poll_interval_max_s)
                continue
            if state == "done":
                return self._page_payloads(objects)
            raise OcrProviderError(
                f"Неизвестное состояние операции Yandex Vision: {state[:100]}"
            )

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
        requested = [int(page) for page in (pages or [])]
        if not requested:
            raise OcrProviderError("Не выбраны страницы для облачного распознавания.")
        on_progress = progress or (lambda *args, **kwargs: None)
        source_digest = hashlib.sha256(Path(pdf_path).read_bytes()).digest()
        planned = self._plan_chunks(pdf_path, requested)
        total_chunks = len(planned)
        collected: list[tuple[int, dict]] = []

        for index, (window, content) in enumerate(planned):
            self._check_cancel(cancel)
            on_progress(index, total_chunks, f"Облачное распознавание: страницы {window[0]}–{window[-1]}")
            warnings: list[str] = []
            parsed = self._recognize_chunk(
                window, content, source_digest, config, api_key, cancel, warnings
            )
            mapped = self._map_pages_to_numbers(parsed, window, warnings)
            collected.extend(mapped)
            for warning in warnings:
                for number in window:
                    collected.append((number, {"_warning": warning}))

        by_page: dict[int, PageOcrResult] = {}
        for number in requested:
            by_page[number] = PageOcrResult(page=number, provides_confidence=False)
        for number, payload in collected:
            target = by_page.setdefault(number, PageOcrResult(page=number, provides_confidence=False))
            warning = payload.get("_warning")
            if warning:
                if warning not in target.errors:
                    target.errors.append(warning)
                continue
            rows = reconstruct_page_rows(payload, self.key)
            target.rows.extend(rows)
            geometry = page_geometry(payload)
            if geometry:
                target.geometry = geometry
        return OcrResult(provider=self.key, pages=[by_page[number] for number in requested])

    def _recognize_chunk(
        self,
        window: list[int],
        content: bytes,
        source_digest: bytes,
        config: dict,
        api_key: str,
        cancel: threading.Event | None,
        warnings: list[str],
    ) -> list:
        key = self._cache_key(source_digest, window, config)
        cached = self._cache_load(key, warnings)
        if cached is not None:
            return cached.get("pages") or []
        pending = self._pending_load(key)
        operation_id = pending["operation_id"] if pending else None
        recognition_base = str(pending.get("recognition_base") or "").strip() if pending else ""
        if operation_id is None:
            operation_id = self._submit(content, config, api_key, cancel, warnings)
            recognition_base = config["vision_base_url"]
            self._pending_save(key, operation_id, window, recognition_base)
        pages_payload = self._poll_recognition(
            operation_id,
            recognition_base or config["vision_base_url"],
            config,
            api_key,
            cancel,
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
