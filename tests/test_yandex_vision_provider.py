from __future__ import annotations

import base64
import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest

from averon_import.core.constants import BASE_COLUMNS
from averon_import.services.ocr.base import OcrProviderError
from averon_import.services.ocr.reconstruction import reconstruct_page_rows
from averon_import.services.ocr.yandex_vision import (
    _HttpFailure,
    YandexVisionProvider,
    _HttpResponse,
)
from averon_import.services.pdf_service import PdfService
from averon_import.services.processing_coordinator import ProcessingCoordinator
from averon_import.services.secrets import MemorySecretStore

API_KEY = "test-key"
BASE_URL = "https://ocr.test/api"
SUBMIT_ROUTE = "/ocr/v1/recognizeTextAsync"
SYNC_ROUTE = "/ocr/v1/recognizeText"
RECOGNITION_ROUTE = "/ocr/v1/getRecognition"
BASE_KEYS = {column["key"] for column in BASE_COLUMNS}


# --------------------------------------------------------------------- helpers


class FakeSettingsService:
    def __init__(self, **overrides):
        values = {
            "folder_id": "folder-test-1",
            "vision_model": "table",
            "vision_base_url": BASE_URL,
            "language_codes": ["ru", "en"],
            "chunk_pages": 2,
            "request_timeout_s": 5.0,
            "operation_timeout_s": 30.0,
        }
        values.update(overrides)
        self.settings = SimpleNamespace(yandex=SimpleNamespace(**values))


def make_pdf(path: Path, page_count: int) -> Path:
    document = fitz.open()
    for _ in range(page_count):
        document.new_page(width=595, height=842)
    document.save(path)
    document.close()
    return path


def word(text: str, x: float, y: float) -> dict:
    return {
        "text": text,
        "boundingBox": {"vertices": [
            {"x": x, "y": y},
            {"x": x + 40, "y": y},
            {"x": x + 40, "y": y + 12},
            {"x": x, "y": y + 12},
        ]},
    }


HEADER_TITLES = [
    "Поз.",
    "Наименование",
    "Тип, марка",
    "Код",
    "Изготовитель",
    "Ед. изм.",
    "Кол.",
    "Масса",
    "Примечание",
]


def table_cell(text: str, row: int, column: int, *, row_span: int = 1, column_span: int = 1) -> dict:
    left = 30 + column * 60
    right = left + 55 * column_span
    top = 40 + row * 20
    bottom = top + 15 * row_span
    return {
        "text": text,
        "rowIndex": row,
        "columnIndex": column,
        "rowSpan": row_span,
        "columnSpan": column_span,
        "boundingBox": {"vertices": [
            {"x": left, "y": top},
            {"x": right, "y": top},
            {"x": right, "y": bottom},
            {"x": left, "y": bottom},
        ]},
    }


def table_page_payload() -> dict:
    """Official primary shape: textAnnotation.tables[].cells[] (no confidence)."""
    cells = [table_cell(title, 0, column) for column, title in enumerate(HEADER_TITLES)]
    data_rows = [
        ["1", "Вентиль проходной", "", "", "", "", "5", "", ""],
        ["2", "Клапан обратный", "", "", "", "", "3", "", ""],
    ]
    for row_index, row_values in enumerate(data_rows, start=1):
        for column, text in enumerate(row_values):
            if text:
                cells.append(table_cell(text, row_index, column))
    return {
        "page": {"width": 595, "height": 842},
        "textAnnotation": {
            "tables": [{"rowCount": 3, "columnCount": 9, "cells": cells}],
        },
    }


def geometry_page_payload() -> dict:
    """Fallback shape: blocks/lines/words geometry without tables."""
    return {
        "page": {"width": 595, "height": 842},
        "textAnnotation": {
            "blocks": [
                {"lines": [
                    {"words": [
                        word("Поз.", 30, 40),
                        word("Наименование", 200, 40),
                        word("Кол.", 480, 40),
                    ]},
                    {"words": [
                        word("1.", 30, 80),
                        word("Вентиль", 200, 80),
                        word("5", 480, 80),
                    ]},
                ]},
            ],
        },
    }


def sync_response(page_payload: dict) -> _HttpResponse:
    return json_response(200, {"result": page_payload})


def json_response(status: int, payload: dict, headers: dict | None = None) -> _HttpResponse:
    return _HttpResponse(status, json.dumps(payload).encode("utf-8"), headers or {})


def jsonl_response(status: int, objects: list[dict], headers: dict | None = None) -> _HttpResponse:
    body = "\n\n".join(
        json.dumps(obj, ensure_ascii=False) for obj in objects
    )
    return _HttpResponse(status, ("\n" + body + "\n\n").encode("utf-8"), headers or {})


SUBMIT_OP = json_response(200, {"id": "op-123", "done": False})
POLL_NOT_DONE = _HttpResponse(200, b"")
DONE_ONE_PAGE = jsonl_response(200, [table_page_payload()])
DONE_TWO_PAGES = jsonl_response(200, [table_page_payload(), table_page_payload()])
SYNC_ONE_PAGE = sync_response(table_page_payload())
SYNC_GEOMETRY_PAGE = sync_response(geometry_page_payload())


class FakeHttp:
    def __init__(self, script=None):
        self.calls: list[dict] = []
        self.script = list(script or [])
        self.default = DONE_ONE_PAGE

    def request(self, method, url, *, body=None, headers=None, timeout=30.0):
        entry = {"method": method, "url": url, "body": body, "headers": headers}
        self.calls.append(entry)
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            if callable(item):
                return item(method=method, url=url, body=body)
            if isinstance(item, tuple):
                status, payload, headers_out = item
                return json_response(status, payload, headers_out)
            return item
        return self.default

    def submit_calls(self) -> list[dict]:
        return [call for call in self.calls if call["method"] == "POST"]

    def get_calls(self) -> list[dict]:
        return [call for call in self.calls if call["method"] == "GET"]

    def submitted_page_counts(self) -> list[int]:
        counts = []
        for call in self.submit_calls():
            payload = json.loads(call["body"])
            raw = base64.b64decode(payload["content"])
            with fitz.open(stream=raw, filetype="pdf") as doc:
                counts.append(doc.page_count)
        return counts

    def submitted_models(self) -> list[str]:
        return [json.loads(call["body"]).get("model") for call in self.submit_calls()]

    def submitted_language_codes(self) -> list[list[str]]:
        return [json.loads(call["body"]).get("languageCodes") for call in self.submit_calls()]


def keyed_store() -> MemorySecretStore:
    store = MemorySecretStore()
    store.set("yandex.api_key", API_KEY)
    return store


def any_pdf(tmp_path: Path) -> Path:
    return make_pdf(tmp_path / f"doc-{abs(hash(tmp_path)) % 10000}.pdf", 12)


def ready_provider(http=None, cache_dir=None, secret_store=None, settings=None, **overrides):
    return YandexVisionProvider(
        settings_service=settings or FakeSettingsService(),
        secret_store=secret_store or keyed_store(),
        cache_dir=cache_dir,
        http=http or FakeHttp(),
        sleep_fn=lambda seconds: None,
        now_fn=lambda: 0.0,
        **overrides,
    )


class TickingClock:
    def __init__(self, start: float = 0.0):
        self.value = start

    def __call__(self) -> float:
        return self.value


def sleeper(clock: TickingClock):
    def _sleep(seconds: float) -> None:
        clock.value += max(seconds, 0.0)
    return _sleep


# ------------------------------------------------------- availability / health


def test_unavailable_without_folder_id(tmp_path):
    provider = ready_provider(settings=FakeSettingsService(folder_id=""))
    assert provider.available() is False
    health = provider.health()
    assert health["available"] is False
    assert health["folder_id_configured"] is False
    assert health["provider"] == "yandex_vision"


def test_unavailable_without_api_key(tmp_path):
    provider = ready_provider(secret_store=MemorySecretStore())
    assert provider.effective_api_key() is None
    assert provider.available() is False
    assert provider.health()["api_key_configured"] is False


def test_available_with_store_key_and_env_override(monkeypatch, tmp_path):
    store = MemorySecretStore()
    store.set("yandex.api_key", API_KEY)
    provider = ready_provider(secret_store=store)
    assert provider.available() is True

    monkeypatch.setenv("AVERON_YANDEX_VISION_API_KEY", "ENV-KEY")
    assert provider.effective_api_key() == "ENV-KEY"


def test_health_makes_no_network_requests_and_hides_secret(monkeypatch, tmp_path):
    monkeypatch.delenv("AVERON_YANDEX_VISION_API_KEY", raising=False)
    monkeypatch.delenv("AVERON_YANDEX_AI_API_KEY", raising=False)
    http = FakeHttp()
    store = MemorySecretStore()
    store.set("yandex.api_key", API_KEY)
    provider = YandexVisionProvider(
        settings_service=FakeSettingsService(),
        secret_store=store,
        cache_dir=tmp_path / "cache",
        http=http,
    )
    payload = json.dumps(provider.health())
    assert http.calls == []
    assert API_KEY not in payload
    assert provider.health()["language_codes"] == ["ru", "en"]


# ------------------------------------------------------------------- chunking


def test_pdf_chunking_respects_chunk_pages(tmp_path):
    pdf = make_pdf(tmp_path / "doc.pdf", 5)
    http = FakeHttp(
        script=[SUBMIT_OP, POLL_NOT_DONE, DONE_TWO_PAGES] * 2
        + [SYNC_ONE_PAGE]
    )
    provider = ready_provider(
        http=http,
        cache_dir=tmp_path / "cache",
        settings=FakeSettingsService(chunk_pages=2),
    )
    result = provider.recognize(pdf, [1, 2, 3, 4, 5])
    assert http.submitted_page_counts() == [2, 2, 1]
    assert http.submitted_models() == ["table", "table", "table"]
    assert [page.page for page in result.pages] == [1, 2, 3, 4, 5]


def test_oversized_chunk_shrinks_until_it_fits(tmp_path):
    pdf = make_pdf(tmp_path / "big.pdf", 6)
    size_two = len(YandexVisionProvider._extract_subset_pdf(pdf, [1, 2]))
    size_three = len(YandexVisionProvider._extract_subset_pdf(pdf, [1, 2, 3]))
    limit = (size_two + size_three) // 2
    assert size_two <= limit < size_three

    http = FakeHttp(script=[SUBMIT_OP, POLL_NOT_DONE, DONE_TWO_PAGES] * 3)
    provider = ready_provider(
        http=http,
        cache_dir=None,
        settings=FakeSettingsService(chunk_pages=4),
        max_file_bytes=limit,
    )
    result = provider.recognize(pdf, [1, 2, 3, 4, 5, 6])
    counts = http.submitted_page_counts()
    assert all(count == 2 for count in counts)
    assert [page.page for page in result.pages] == [1, 2, 3, 4, 5, 6]


def test_single_page_overflow_raises_clear_error(tmp_path):
    pdf = make_pdf(tmp_path / "one.pdf", 1)
    http = FakeHttp()
    provider = ready_provider(http=http, cache_dir=tmp_path / "cache", max_file_bytes=100)
    with pytest.raises(OcrProviderError, match="превышает лимит"):
        provider.recognize(pdf, [1])
    assert http.submit_calls() == []


def test_selected_pages_preserve_original_numbers(tmp_path):
    pdf = any_pdf(tmp_path)
    http = FakeHttp(script=[SUBMIT_OP, POLL_NOT_DONE, DONE_TWO_PAGES, SYNC_ONE_PAGE])
    provider = ready_provider(
        http=http,
        cache_dir=tmp_path / "cache",
        settings=FakeSettingsService(chunk_pages=8),
    )
    result = provider.recognize(pdf, [4, 10, 11])
    assert [page.page for page in result.pages] == [4, 10, 11]
    assert sum(http.submitted_page_counts()) == 3


# ------------------------------------------------------------ v1 REST contract


def test_single_page_uses_sync_route_without_async_polling(tmp_path):
    pdf = any_pdf(tmp_path)
    http = FakeHttp(script=[SYNC_ONE_PAGE])
    updates: list[str] = []
    provider = ready_provider(http=http, cache_dir=tmp_path / "cache")
    provider.recognize(pdf, [1], progress=lambda _i, _t, message: updates.append(message))
    assert http.submit_calls()[0]["url"] == f"{BASE_URL}{SYNC_ROUTE}"
    assert http.get_calls() == []
    assert updates[-3:] == [
        "Yandex OCR: отправляем страницу",
        "Yandex OCR: распознаём страницу",
        "Yandex OCR: результат получен",
    ]


def test_two_pages_keep_async_submit_and_direct_polling(tmp_path):
    pdf = any_pdf(tmp_path)
    http = FakeHttp(script=[SUBMIT_OP, POLL_NOT_DONE, DONE_TWO_PAGES])
    provider = ready_provider(
        http=http,
        cache_dir=tmp_path / "cache",
        settings=FakeSettingsService(chunk_pages=2),
    )
    result = provider.recognize(pdf, [1, 2])
    assert result.pages[0].rows and result.pages[1].rows
    assert http.submit_calls()[0]["url"] == f"{BASE_URL}{SUBMIT_ROUTE}"
    assert all(call["url"] != f"{BASE_URL}{SYNC_ROUTE}" for call in http.submit_calls())
    assert len(http.get_calls()) == 2


def test_auth_headers_api_key_and_folder_id(tmp_path):
    pdf = any_pdf(tmp_path)
    http = FakeHttp(script=[SYNC_ONE_PAGE])
    provider = ready_provider(http=http, cache_dir=tmp_path / "cache")
    provider.recognize(pdf, [1])
    for call in http.calls:
        headers = call["headers"]
        assert headers["Authorization"] == f"Api-Key {API_KEY}"
        assert headers["x-folder-id"] == "folder-test-1"
        assert "Bearer" not in headers["Authorization"]


def test_language_codes_sent_and_configurable(tmp_path):
    pdf = any_pdf(tmp_path)
    http = FakeHttp(script=[SYNC_ONE_PAGE])
    provider = ready_provider(http=http, cache_dir=tmp_path / "cache")
    provider.recognize(pdf, [1])
    assert http.submitted_language_codes() == [["ru", "en"]]

    http_ru = FakeHttp(script=[SYNC_ONE_PAGE])
    provider_ru = ready_provider(
        http=http_ru,
        cache_dir=None,
        settings=FakeSettingsService(language_codes=["ru"]),
    )
    provider_ru.recognize(make_pdf(tmp_path / "other.pdf", 12), [2])
    assert http_ru.submitted_language_codes() == [["ru"]]


def test_multiple_missing_critical_cells_use_one_secondary_request(tmp_path):
    cells = [table_cell(title, 0, column) for column, title in enumerate(HEADER_TITLES)]
    row_values = ["1", "Клапан обратный", "V-1", "", "", "шт.", "", "", ""]
    cells.extend(
        table_cell(text, 1, column)
        for column, text in enumerate(row_values)
    )
    primary = {
        "page": {"width": 595, "height": 842},
        "textAnnotation": {
            "width": 595,
            "height": 842,
            "tables": [{
                "rowCount": 2,
                "columnCount": 9,
                "boundingBox": {"vertices": [
                    {"x": 20, "y": 35}, {"x": 575, "y": 35},
                    {"x": 575, "y": 75}, {"x": 20, "y": 75},
                ]},
                "cells": cells,
            }],
        },
    }
    http = FakeHttp(script=[sync_response(primary), SYNC_ONE_PAGE])
    provider = ready_provider(http=http, cache_dir=tmp_path / "cache")

    result = provider.recognize(any_pdf(tmp_path), [1])

    assert len(http.submit_calls()) == 2
    secondary_body = json.loads(http.submit_calls()[1]["body"])
    assert secondary_body["mimeType"] == "image/png"
    assert secondary_body["model"] == "table"
    assert result.stats["primary_requests"] == 1
    assert result.stats["secondary_requests"] == 1
    assert result.stats["secondary_candidates"] == 0


def test_sync_and_async_cache_strategies_are_isolated(tmp_path):
    pdf = any_pdf(tmp_path)
    provider = ready_provider(http=FakeHttp(script=[]), cache_dir=tmp_path / "cache")
    source_digest = hashlib.sha256(pdf.read_bytes()).digest()
    config = provider._yandex_settings()
    sync_key = provider._cache_key(source_digest, [1], config, strategy="sync")
    async_key = provider._cache_key(source_digest, [1], config, strategy="async")
    assert sync_key != async_key


def test_sync_pending_metadata_does_not_trigger_async_polling(tmp_path):
    pdf = any_pdf(tmp_path)
    cache_dir = tmp_path / "cache"
    http = FakeHttp(script=[SYNC_ONE_PAGE])
    provider = ready_provider(http=http, cache_dir=cache_dir)
    source_digest = hashlib.sha256(pdf.read_bytes()).digest()
    async_key = provider._cache_key(
        source_digest, [1], provider._yandex_settings(), strategy="async"
    )
    (cache_dir / "pending" / f"{async_key}.json").write_text(
        json.dumps({"operation_id": "old-op", "pages": [1]}),
        encoding="utf-8",
    )

    result = provider.recognize(pdf, [1])

    assert result.pages[0].rows
    assert [call["url"] for call in http.submit_calls()] == [f"{BASE_URL}{SYNC_ROUTE}"]
    assert http.get_calls() == []


def test_jsonl_multi_page_yields_one_result_per_line(tmp_path):
    pdf = any_pdf(tmp_path)
    done = jsonl_response(200, [geometry_page_payload(), table_page_payload()])
    http = FakeHttp(script=[
        SUBMIT_OP,
        POLL_NOT_DONE,
        done,
    ])
    provider = ready_provider(
        http=http,
        cache_dir=tmp_path / "cache",
        settings=FakeSettingsService(chunk_pages=8),
    )
    result = provider.recognize(pdf, [4, 10])
    assert [page.page for page in result.pages] == [4, 10]
    assert all(page.rows for page in result.pages)


def test_pending_metadata_carries_operation_id_and_recognition_base(tmp_path):
    pdf = any_pdf(tmp_path)
    cache_dir = tmp_path / "cache"
    observed: list[dict] = []

    def snapshot_pending(**kwargs):
        for path in sorted((cache_dir / "pending").glob("*.json")):
            observed.append(json.loads(path.read_text(encoding="utf-8")))
        return POLL_NOT_DONE

    http = FakeHttp(script=[SUBMIT_OP, snapshot_pending, DONE_TWO_PAGES])
    provider = ready_provider(http=http, cache_dir=cache_dir)
    result = provider.recognize(pdf, [1, 2])
    assert result.pages[0].rows
    assert len(observed) == 1
    assert observed[0]["operation_id"] == "op-123"
    assert observed[0]["recognition_base"] == BASE_URL
    assert observed[0]["pages"] == [1, 2]
    assert list((cache_dir / "pending").glob("*.json")) == []


# ------------------------------------------------------------------ parsing


def test_table_cells_primary_map_to_base_columns(tmp_path):
    from averon_import.core.normalizers import normalize_cell

    http = FakeHttp(script=[SYNC_ONE_PAGE])
    provider = ready_provider(http=http, cache_dir=tmp_path / "cache")
    result = provider.recognize(any_pdf(tmp_path), [7])
    page_result = result.pages[0]
    assert page_result.provides_confidence is False
    assert page_result.geometry == {"width": 595, "height": 842}
    rows = page_result.rows
    assert len(rows) == 2
    first = rows[0]
    assert set(first.values) <= BASE_KEYS
    assert first.values.get("position") == normalize_cell("position", "1")
    assert first.values.get("name") == normalize_cell("name", "Вентиль проходной")
    assert first.values.get("quantity") == normalize_cell("quantity", "5")
    assert first.confidences == {}
    assert all(value == "yandex_vision" for value in first.sources.values())


def test_sync_geometry_fallback_maps_rows_without_confidence(tmp_path):
    http = FakeHttp(script=[SYNC_GEOMETRY_PAGE])
    provider = ready_provider(http=http, cache_dir=tmp_path / "cache")

    result = provider.recognize(any_pdf(tmp_path), [7])

    page_result = result.pages[0]
    assert page_result.rows
    assert page_result.rows[0].values["position"] == "1."
    assert page_result.rows[0].values["quantity"] == "5"
    assert all(row.confidences == {} for row in page_result.rows)


def test_table_spans_are_respected_in_primary_path():
    cells = [table_cell(title, 0, column) for column, title in enumerate(HEADER_TITLES)]
    cells.append(table_cell("шт.", 0, 5))  # placeholder replaced below
    cells[-1] = table_cell("Комплектующие", 1, 1, column_span=3)
    cells.append(table_cell("1", 1, 0))
    cells.append(table_cell("12", 2, 0))
    cells.append(table_cell("шт.", 2, 5))
    payload = {
        "page": {"width": 595, "height": 842},
        "textAnnotation": {
            "tables": [{"rowCount": 3, "columnCount": 9, "cells": cells}],
        },
    }
    rows = reconstruct_page_rows(payload, "yandex_vision")
    assert len(rows) == 2
    # columnSpan=3 cell lands on its start column key (name), not duplicated
    assert rows[0].values["name"]
    assert rows[0].values["position"] == "1"
    assert rows[1].values["position"] == "12"
    assert all(row.confidences == {} for row in rows)


def test_rowspan_cell_covers_following_rows():
    cells = [table_cell(title, 0, column) for column, title in enumerate(HEADER_TITLES)]
    cells.append(table_cell("1", 1, 0))
    cells.append(table_cell("Болт М12", 1, 1))
    cells.append(table_cell("шт.", 1, 5, row_span=2))
    cells.append(table_cell("2", 2, 0))
    cells.append(table_cell("Гайка М12", 2, 1))
    payload = {
        "page": {"width": 595, "height": 842},
        "textAnnotation": {
            "tables": [{"rowCount": 3, "columnCount": 9, "cells": cells}],
        },
    }
    rows = reconstruct_page_rows(payload, "yandex_vision")
    assert [row.values.get("unit") for row in rows] == ["шт.", "шт."]
    assert all(row.confidences == {} for row in rows)


def test_direct_positional_mapping_when_header_unrecognised():
    cells = [
        table_cell(f"C{column}", 0, column) for column in range(9)
    ]
    cells.append(table_cell("1", 1, 0))
    cells.append(table_cell("Болт М12", 1, 1))
    cells.append(table_cell("10", 1, 6))
    payload = {
        "page": {"width": 595, "height": 842},
        "textAnnotation": {
            "tables": [{"rowCount": 2, "columnCount": 9, "cells": cells}],
        },
    }
    rows = reconstruct_page_rows(payload, "yandex_vision")
    data = [row for row in rows if row.values.get("name") == "Болт М12"]
    assert data
    assert data[0].values["quantity"] == "10"
    assert data[0].values["position"] == "1"


def test_unmappable_narrow_table_returns_nothing_instead_of_guesses():
    payload = {
        "page": {"width": 595, "height": 842},
        "textAnnotation": {
            "tables": [{
                "rowCount": 2,
                "columnCount": 3,
                "cells": [
                    table_cell("A", 0, 0),
                    table_cell("B", 0, 1),
                    table_cell("C", 0, 2),
                    table_cell("x", 1, 0),
                    table_cell("y", 1, 1),
                    table_cell("z", 1, 2),
                ],
            }],
        },
    }
    rows = reconstruct_page_rows(payload, "yandex_vision")
    # narrow table without header and without 9 columns cannot be mapped;
    # geometry/plain-text fallbacks produce no fabricated columns either
    assert all(set(row.values) <= BASE_KEYS for row in rows)


def test_geometry_fallback_when_tables_absent():
    rows_first = reconstruct_page_rows(geometry_page_payload(), "yandex_vision")
    rows_second = reconstruct_page_rows(geometry_page_payload(), "yandex_vision")
    assert rows_first[0].values["position"] == "1."
    assert rows_first[0].values["quantity"] == "5"
    assert set(rows_first[0].values) <= BASE_KEYS
    assert rows_first == rows_second
    assert all(row.confidences == {} for row in rows_first)


def test_plain_text_last_resort_without_geometry():
    payload = {
        "page": {"width": 595, "height": 842},
        "textAnnotation": {"text": "Болт М12x60\n\nГайка М12"},
    }
    rows = reconstruct_page_rows(payload, "yandex_vision")
    assert [row.values["name"] for row in rows] == ["Болт М12×60", "Гайка М12"]
    assert all(row.bbox == {} for row in rows)


def test_no_synthetic_confidence_anywhere():
    for payload in (table_page_payload(), geometry_page_payload()):
        rows = reconstruct_page_rows(payload, "yandex_vision")
        assert rows
        assert all(row.confidences == {} for row in rows)


def test_normalization_applied_exactly_once(tmp_path):
    from averon_import.core.normalizers import normalize_cell

    raw_line = "1   Вентиль   проходной"
    payload = {
        "page": {"width": 595, "height": 842},
        "textAnnotation": {
            "blocks": [{"lines": [{"words": [word(raw_line, 50, 300)]}]}],
        },
    }
    rows = reconstruct_page_rows(payload, "yandex_vision")
    assert rows[0].values["name"] == normalize_cell("name", raw_line)


# --------------------------------------------------------------------- cache


def test_cache_hit_avoids_second_network_call(tmp_path):
    pdf = any_pdf(tmp_path)
    http = FakeHttp(script=[SYNC_ONE_PAGE])
    provider = ready_provider(http=http, cache_dir=tmp_path / "cache")
    first = provider.recognize(pdf, [1])
    calls_after_first = len(http.calls)
    second = provider.recognize(pdf, [1])
    assert len(http.calls) == calls_after_first
    assert [row.source_row for row in first.pages[0].rows] == [
        row.source_row for row in second.pages[0].rows
    ]


def test_cache_key_changes_with_language_codes(tmp_path):
    pdf = any_pdf(tmp_path)
    provider = ready_provider(http=FakeHttp(script=[]), cache_dir=tmp_path / "cache")
    source_digest = hashlib.sha256(pdf.read_bytes()).digest()
    base_config = provider._yandex_settings()
    common_config = {
        "vision_base_url": base_config["vision_base_url"],
        "vision_model": base_config["vision_model"],
        "folder_id": base_config["folder_id"],
    }

    key_ru_en = provider._cache_key(
        source_digest,
        [1, 2],
        {**common_config, "language_codes": ["ru", "en"]},
    )
    key_any_language = provider._cache_key(
        source_digest,
        [1, 2],
        {**common_config, "language_codes": ["*"]},
    )

    assert key_ru_en != key_any_language


def test_cached_format_matches_jsonl_page_payloads(tmp_path):
    pdf = any_pdf(tmp_path)
    http = FakeHttp(script=[SYNC_ONE_PAGE])
    cache_dir = tmp_path / "cache"
    provider = ready_provider(http=http, cache_dir=cache_dir)
    provider.recognize(pdf, [1])
    files = list((cache_dir / "results").glob("*.json"))
    assert len(files) == 1
    stored = json.loads(files[0].read_text(encoding="utf-8"))
    assert stored["version"] == 3
    assert stored["provider"] == "yandex_vision"
    assert isinstance(stored["pages"], list)
    for payload in stored["pages"]:
        assert isinstance(payload.get("textAnnotation"), dict)


def test_corrupted_or_outdated_cache_is_ignored_with_warning(tmp_path):
    pdf = any_pdf(tmp_path)
    http = FakeHttp(script=[SYNC_ONE_PAGE] * 3)
    cache_dir = tmp_path / "cache"
    provider = ready_provider(http=http, cache_dir=cache_dir)
    provider.recognize(pdf, [1])
    paths = list((cache_dir / "results").glob("*.json"))

    paths[0].write_text("{broken json", encoding="utf-8")
    result_broken = provider.recognize(pdf, [1])

    outdated = json.loads(paths[0].read_text(encoding="utf-8"))
    outdated["version"] = 1
    paths[0].write_text(json.dumps(outdated), encoding="utf-8")
    result_outdated = provider.recognize(pdf, [1])

    warnings = [item for page in (result_broken.pages + result_outdated.pages) for item in page.errors]
    assert any("повреждённый" in warning.lower() for warning in warnings)
    assert len(http.submit_calls()) >= 2
    assert result_outdated.pages[0].rows


# --------------------------------------------------------------- retry policy


def test_http_401_fails_fast_without_retry(tmp_path):
    http = FakeHttp(script=[(401, {"message": "unauthorized"}, {})])
    provider = ready_provider(http=http, cache_dir=tmp_path / "cache")
    with pytest.raises(OcrProviderError, match="401"):
        provider.recognize(any_pdf(tmp_path), [1])
    assert len(http.calls) == 1


def test_http_403_fails_fast_with_configuration_hint(tmp_path):
    http = FakeHttp(script=[(403, {"message": "forbidden"}, {})])
    provider = ready_provider(http=http, cache_dir=tmp_path / "cache")
    with pytest.raises(OcrProviderError, match="403"):
        provider.recognize(any_pdf(tmp_path), [1])


def test_http_429_is_retried_then_succeeds(tmp_path):
    pdf = any_pdf(tmp_path)
    http = FakeHttp(script=[
        (429, {"message": "rate limited"}, {"Retry-After": "0"}),
        SYNC_ONE_PAGE,
    ])
    provider = ready_provider(http=http, cache_dir=tmp_path / "cache")
    result = provider.recognize(pdf, [1])
    assert len(http.submit_calls()) == 2
    assert result.pages[0].rows


def test_sync_network_timeout_retries_then_returns_clear_error(tmp_path):
    http = FakeHttp(
        script=[
            _HttpFailure(0, "request timed out"),
            _HttpFailure(0, "request timed out"),
        ]
    )
    provider = ready_provider(
        http=http,
        cache_dir=tmp_path / "cache",
        submit_attempts=2,
    )

    with pytest.raises(OcrProviderError, match="Синхронный OCR.*timeout"):
        provider.recognize(any_pdf(tmp_path), [1])

    assert len(http.submit_calls()) == 2


def test_http_500_retries_limited_then_raises(tmp_path):
    http = FakeHttp(script=[
        (500, {"message": "boom"}, {}),
        (500, {"message": "boom"}, {}),
    ])
    provider = YandexVisionProvider(
        settings_service=FakeSettingsService(),
        secret_store=keyed_store(),
        cache_dir=tmp_path / "cache",
        submit_attempts=2,
        sleep_fn=lambda seconds: None,
        now_fn=lambda: 0.0,
        http=http,
    )
    with pytest.raises(OcrProviderError, match="HTTP 500"):
        provider.recognize(any_pdf(tmp_path), [1])
    assert len(http.submit_calls()) == 2


def test_polling_errors_are_retryable_but_timeout_wins(tmp_path):
    pdf = any_pdf(tmp_path)
    clock = TickingClock()
    http = FakeHttp(script=[
        (503, {"message": "temporarily"}, {}),
        SUBMIT_OP,
        POLL_NOT_DONE,
        POLL_NOT_DONE,
        POLL_NOT_DONE,
    ])
    provider = YandexVisionProvider(
        settings_service=FakeSettingsService(operation_timeout_s=15.0),
        secret_store=keyed_store(),
        cache_dir=tmp_path / "cache",
        poll_interval_s=10.0,
        sleep_fn=sleeper(clock),
        now_fn=clock,
        http=http,
    )
    with pytest.raises(OcrProviderError, match="время ожидания"):
        provider.recognize(pdf, [1, 2])
    assert len([c for c in http.calls if c["method"] == "GET"]) == 3


def test_operation_error_state_raises_clear_message(tmp_path):
    pdf = any_pdf(tmp_path)
    failed = jsonl_response(200, [{"state": "ERROR", "error": {"message": "quota exceeded"}}])
    http = FakeHttp(script=[SUBMIT_OP, failed])
    provider = ready_provider(http=http, cache_dir=tmp_path / "cache")
    with pytest.raises(OcrProviderError, match="quota exceeded"):
        provider.recognize(pdf, [1, 2])


# ----------------------------------------------------------- async operations


def test_direct_polling_pending_pending_success_reports_progress(tmp_path):
    pdf = any_pdf(tmp_path)
    clock = TickingClock()
    http = FakeHttp(
        script=[
            SUBMIT_OP,
            POLL_NOT_DONE,
            POLL_NOT_DONE,
            DONE_TWO_PAGES,
        ]
    )
    updates: list[tuple[int, int, str]] = []
    provider = YandexVisionProvider(
        settings_service=FakeSettingsService(operation_timeout_s=30.0),
        secret_store=keyed_store(),
        cache_dir=tmp_path / "cache",
        poll_interval_s=1.0,
        sleep_fn=sleeper(clock),
        now_fn=clock,
        http=http,
    )

    result = provider.recognize(
        pdf,
        [1, 2],
        progress=lambda index, total, message: updates.append((index, total, message)),
    )

    assert result.pages[0].rows
    assert [call["url"] for call in http.get_calls()] == [
        f"{BASE_URL}{RECOGNITION_ROUTE}?operationId=op-123",
        f"{BASE_URL}{RECOGNITION_ROUTE}?operationId=op-123",
        f"{BASE_URL}{RECOGNITION_ROUTE}?operationId=op-123",
    ]
    assert clock.value == 3.0
    assert updates[-2:] == [
        (0, 1, "Yandex OCR: ожидаем результат, 0 с"),
        (0, 1, "Yandex OCR: ожидаем результат, 1 с"),
    ]


def test_transient_not_ready_recognition_404_is_polled_again(tmp_path):
    pdf = any_pdf(tmp_path)
    not_ready = (
        404,
        {"error": {"grpcCode": 5, "message": "operation data is not ready"}},
        {},
    )
    http = FakeHttp(
        script=[SUBMIT_OP, not_ready, not_ready, DONE_TWO_PAGES]
    )
    provider = ready_provider(
        http=http,
        cache_dir=tmp_path / "cache",
        poll_interval_s=1.0,
    )

    result = provider.recognize(pdf, [1, 2])

    assert result.pages[0].rows
    assert [call["url"] for call in http.get_calls()] == [
        f"{BASE_URL}{RECOGNITION_ROUTE}?operationId=op-123",
        f"{BASE_URL}{RECOGNITION_ROUTE}?operationId=op-123",
        f"{BASE_URL}{RECOGNITION_ROUTE}?operationId=op-123",
    ]


def test_empty_recognition_body_is_pending(tmp_path):
    pdf = any_pdf(tmp_path)
    http = FakeHttp(script=[SUBMIT_OP, _HttpResponse(200, b""), DONE_TWO_PAGES])
    provider = ready_provider(http=http, cache_dir=tmp_path / "cache")

    result = provider.recognize(pdf, [1, 2])

    assert result.pages[0].rows
    assert len(http.get_calls()) == 2


def test_other_recognition_404_remains_fatal(tmp_path):
    pdf = any_pdf(tmp_path)
    http = FakeHttp(
        script=[
            SUBMIT_OP,
            (404, {"error": {"message": "operation not found"}}, {}),
        ]
    )
    provider = ready_provider(http=http, cache_dir=tmp_path / "cache")

    with pytest.raises(OcrProviderError, match="HTTP 404"):
        provider.recognize(pdf, [1, 2])


def test_restart_resumes_polling_without_new_submit(tmp_path):
    pdf = any_pdf(tmp_path)
    cache_dir = tmp_path / "cache"
    clock = TickingClock()

    def make(script):
        http = FakeHttp(script=list(script))
        provider = YandexVisionProvider(
            settings_service=FakeSettingsService(operation_timeout_s=15.0),
            secret_store=keyed_store(),
            cache_dir=cache_dir,
            poll_interval_s=10.0,
            submit_attempts=1,
            sleep_fn=sleeper(clock),
            now_fn=clock,
            http=http,
        )
        return http, provider

    first_http, first_provider = make([SUBMIT_OP, POLL_NOT_DONE, POLL_NOT_DONE, POLL_NOT_DONE])
    with pytest.raises(OcrProviderError, match="время ожидания"):
        first_provider.recognize(pdf, [1, 2])
    assert len(first_http.submit_calls()) == 1

    second_http, second_provider = make([POLL_NOT_DONE, DONE_TWO_PAGES])
    result = second_provider.recognize(pdf, [1, 2])
    assert second_http.submit_calls() == []
    assert [call["method"] for call in second_http.calls] == ["GET", "GET"]
    for call in second_http.get_calls():
        assert call["url"] == f"{BASE_URL}{RECOGNITION_ROUTE}?operationId=op-123"
    assert result.pages[0].rows


def test_cancellation_between_poll_iterations(tmp_path):
    pdf = any_pdf(tmp_path)
    cancel = threading.Event()

    def set_cancel(seconds: float) -> None:
        cancel.set()

    http = FakeHttp(script=[SUBMIT_OP, POLL_NOT_DONE])
    provider = YandexVisionProvider(
        settings_service=FakeSettingsService(operation_timeout_s=60.0),
        secret_store=keyed_store(),
        cache_dir=tmp_path / "cache",
        sleep_fn=set_cancel,
        now_fn=lambda: 0.0,
        http=http,
    )
    with pytest.raises(OcrProviderError, match="отменено"):
        provider.recognize(pdf, [1, 2], cancel=cancel)


# ------------------------------------------------------------ secret hygiene


def test_api_key_never_leaks_into_results_errors_or_cache(tmp_path):
    pdf = any_pdf(tmp_path)
    http = FakeHttp(script=[(401, {"message": "bad key"}, {})])
    cache_dir = tmp_path / "cache"
    provider = ready_provider(http=http, cache_dir=cache_dir)
    with pytest.raises(OcrProviderError) as exc_info:
        provider.recognize(pdf, [1])
    assert API_KEY not in str(exc_info.value)
    for path in cache_dir.rglob("*"):
        if path.is_file():
            assert API_KEY not in path.read_text(encoding="utf-8", errors="ignore")


def test_successful_result_payload_contains_no_secret(tmp_path):
    pdf = any_pdf(tmp_path)
    http = FakeHttp(script=[SYNC_ONE_PAGE])
    cache_dir = tmp_path / "cache"
    provider = ready_provider(http=http, cache_dir=cache_dir)
    result = provider.recognize(pdf, [1])
    serialized = json.dumps(
        [row.as_dict() for row in result.pages[0].rows], ensure_ascii=False
    )
    assert API_KEY not in serialized
    for path in (cache_dir / "results").glob("*.json"):
        assert API_KEY not in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- coordinator


def test_coordinator_cloud_status_follows_configuration(tmp_path):
    ready = ready_provider(cache_dir=tmp_path / "c1")
    configured = ProcessingCoordinator(PdfService(), smart_ai=object(), providers={"cloud": ready})
    assert configured.mode_status()["cloud"] == "ready"

    empty = ready_provider(settings=FakeSettingsService(folder_id=""))
    not_configured = ProcessingCoordinator(PdfService(), smart_ai=object(), providers={"cloud": empty})
    assert not_configured.mode_status()["cloud"] == "not_configured"
