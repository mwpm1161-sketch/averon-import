from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from averon_import.services.sourcing.models import ProductIntent
from averon_import.services.sourcing.providers.base import SourcingProviderError
from averon_import.services.sourcing.providers.tstn import (
    TSTN_MAX_SYNC_PAGES,
    TstnApiClient,
    TstnCatalogMirror,
    TstnHttpResponse,
    TstnItemDetail,
    TstnItemListResponse,
    normalize_tstn_search_text,
)


SYNTHETIC_TOKEN = "tn2-offline-synthetic-access-token"


def item(
    item_id: int | None,
    *,
    article: str | None,
    name: str = "Гибкая черепица Джаз",
    brand: str | None = "ТЕХНОНИКОЛЬ",
    subbrand: str | None = None,
    category_id: int = 10,
    category_name: str = "Кровельные материалы",
    units_list=None,
) -> TstnItemDetail:
    categories = None if category_id is None else [{"id": category_id, "name": category_name, "parent": 1}]
    return TstnItemDetail.model_validate({
        "id": item_id,
        "article": article,
        "name": name,
        "brand": brand,
        "subbrand": subbrand,
        "categories": categories,
        "barcode": "0004650069952664",
        "base_unit": "упак",
        "units_list": units_list,
        "date_create": "25.01.2020 22:50:05",
        "date_modify": "26.01.2020 22:50:05",
        "country_supplier": "Россия",
    })


def page(
    rows,
    *,
    offset: int,
    limit: int,
    count_all: int | None,
    count: int | None = None,
    page_num: int | None = None,
) -> TstnItemListResponse:
    return TstnItemListResponse.model_validate({
        "count": len(rows) if count is None else count,
        "count_all": count_all,
        "page_num": offset // limit + 1 if page_num is None else page_num,
        "results": rows,
    })


class FakeTstnClient:
    def __init__(self, pages_by_offset, *, failure_by_offset=None):
        self.pages_by_offset = dict(pages_by_offset)
        self.failure_by_offset = dict(failure_by_offset or {})
        self.calls: list[tuple[str, int, int]] = []

    def list_items(self, *, access_token, limit, offset):
        assert access_token == SYNTHETIC_TOKEN
        self.calls.append((access_token, limit, offset))
        if offset in self.failure_by_offset:
            raise self.failure_by_offset[offset]
        if offset not in self.pages_by_offset:
            return page([], offset=offset, limit=limit, count_all=offset)
        return self.pages_by_offset[offset]


def paged_client(rows, *, page_size=2, count_all=None):
    total = len(rows) if count_all is None else count_all
    pages = {}
    for offset in range(0, len(rows), page_size):
        pages[offset] = page(
            rows[offset : offset + page_size],
            offset=offset,
            limit=page_size,
            count_all=total,
        )
    # A final empty page is intentional for a catalog whose size is an exact
    # multiple of page_size; it proves that traversal ended without count_all.
    if len(rows) % page_size == 0:
        pages[len(rows)] = page([], offset=len(rows), limit=page_size, count_all=total)
    return FakeTstnClient(pages)


def sync(mirror, client, *, page_size=2, max_pages=8, **kwargs):
    return mirror.sync(
        client,
        access_token=SYNTHETIC_TOKEN,
        page_size=page_size,
        max_pages=max_pages,
        **kwargs,
    )


def test_multi_page_sync_builds_local_index_and_survives_restart(tmp_path):
    rows = [
        item(1001, article="001755", name="Гибкая черепица «Джаз»"),
        item(1002, article="000042", name="Планка карнизная", subbrand="Шинглас"),
        item(1003, article="000043", name="Кровельный конек"),
    ]
    client = paged_client(rows)
    path = tmp_path / "tstn.sqlite3"
    mirror = TstnCatalogMirror(path)

    result = sync(mirror, client)

    assert result.changed is True
    assert result.item_count == 3
    assert result.page_count == 2
    assert mirror.search_index_ready is True
    assert mirror.check_integrity() is True
    assert [row.article for row in mirror.search(article="001755")] == ["001755"]
    assert [row.id for row in mirror.search(item_id="1001")] == ["1001"]
    reopened = TstnCatalogMirror(path)
    assert reopened.revision == result.revision
    assert reopened.search_index_revision == result.search_index_revision
    assert reopened.search(article="000042")[0].name == "Планка карнизная"
    assert reopened.stats()["item_count"] == 3


def test_empty_catalog_requires_and_accepts_an_explicit_zero_total(tmp_path):
    mirror = TstnCatalogMirror(tmp_path / "empty.sqlite3")
    client = FakeTstnClient({0: page([], offset=0, limit=2, count_all=0)})

    result = sync(mirror, client)

    assert result.changed is True
    assert result.item_count == 0
    assert result.page_count == 1
    assert mirror.check_integrity() is True
    assert mirror.search(article="absent") == []


def test_same_complete_snapshot_keeps_catalog_revision(tmp_path):
    mirror = TstnCatalogMirror(tmp_path / "same.sqlite3")
    client = paged_client([item(10, article="000010", name="Одинаковый снимок")])

    first = sync(mirror, client)
    second = sync(mirror, client)

    assert first.changed is True
    assert second.changed is False
    assert second.revision == first.revision


def test_pagination_uses_typed_client_and_bounded_limit_offset_calls(tmp_path):
    rows = [item(i, article=f"00{i}") for i in range(1, 6)]
    client = paged_client(rows)
    mirror = TstnCatalogMirror(tmp_path / "tstn.sqlite3")

    result = sync(mirror, client)

    assert result.page_count == 3
    assert [call[1:] for call in client.calls] == [(2, 0), (2, 2), (2, 4)]
    assert all(call[0] == SYNTHETIC_TOKEN for call in client.calls)
    assert result.item_count == 5


def test_sync_uses_tn1_typed_client_with_only_an_offline_fake_transport(tmp_path):
    rows = [
        {"id": 41, "article": "000041", "name": "Позиция первая"},
        {"id": 42, "article": "000042", "name": "Позиция вторая"},
        {"id": 43, "article": "000043", "name": "Позиция третья"},
    ]

    class FakePageTransport:
        def __init__(self):
            self.calls: list[tuple[int, int]] = []

        def __call__(self, request, timeout):
            query = parse_qs(urlsplit(request.full_url).query)
            assert query["access_token"] == [SYNTHETIC_TOKEN]
            limit = int(query["limit"][0])
            offset = int(query["offset"][0])
            self.calls.append((limit, offset))
            selected = rows[offset : offset + limit]
            response = {
                "count": len(selected),
                "count_all": len(rows),
                "page_num": offset // limit + 1,
                "results": selected,
            }
            return TstnHttpResponse(
                status_code=200,
                body=json.dumps(response, ensure_ascii=False).encode("utf-8"),
            )

    transport = FakePageTransport()
    typed_client = TstnApiClient(transport=transport)
    mirror = TstnCatalogMirror(tmp_path / "tstn.sqlite3")

    result = sync(mirror, typed_client, page_size=2)

    assert result.item_count == 3
    assert transport.calls == [(2, 0), (2, 2)]
    assert [record.article for record in mirror.search(article="000043")] == ["000043"]


def test_search_normalizes_russian_case_punctuation_and_whitespace_without_mutating_source(tmp_path):
    product = item(
        501,
        article="001-755",
        name="ГИБКАЯ   ЧЕРЕПИЦА, «Джаз»",
        brand="ТЕХНОНИКОЛЬ",
        subbrand="ШИНГЛАС",
        category_name="Кровельные, материалы",
    )
    mirror = TstnCatalogMirror(tmp_path / "tstn.sqlite3")
    sync(mirror, paged_client([product], page_size=2), page_size=2)

    assert mirror.search(name="гибкая черепица джаз")[0].name == "ГИБКАЯ   ЧЕРЕПИЦА, «Джаз»"
    assert mirror.search(brand="технониколь", subbrand="шинглас")
    assert mirror.search(category="кровельные материалы")
    assert mirror.search(name="ДЖАЗ")
    assert normalize_tstn_search_text(" Ёлка — кровля! ") == "елка кровля"


def test_opaque_units_list_is_stored_but_not_used_as_search_attributes(tmp_path):
    mirror = TstnCatalogMirror(tmp_path / "tstn.sqlite3")
    product = item(502, article="000502", name="Черепица", units_list=[{"undocumented": "opaque-field"}])
    sync(mirror, paged_client([product], page_size=2), page_size=2)

    assert mirror.search(name="opaque field") == []
    assert mirror.search(article="000502")[0].units_list == ({"undocumented": "opaque-field"},)


def test_article_code_normalization_preserves_leading_zeroes_and_id_is_separate(tmp_path):
    mirror = TstnCatalogMirror(tmp_path / "tstn.sqlite3")
    rows = [
        item(1755, article="001755", name="По артикулу"),
        item(1, article="1755", name="По другому артикулу"),
    ]
    sync(mirror, paged_client(rows), page_size=2)

    assert [row.article for row in mirror.search(article="001755")] == ["001755"]
    assert [row.article for row in mirror.search(article="001 755")] == ["001755"]
    assert mirror.search(article="1755")[0].name == "По другому артикулу"
    assert mirror.search(item_id="001755") == []
    assert mirror.search(item_id="1755")[0].article == "001755"


def test_distinct_product_ids_with_same_article_remain_distinct_candidates(tmp_path):
    mirror = TstnCatalogMirror(tmp_path / "tstn.sqlite3")
    rows = [
        item(10, article="0007", name="Вариант A"),
        item(11, article="0007", name="Вариант B"),
    ]
    sync(mirror, paged_client(rows), page_size=2)

    found = mirror.search(article="0007")
    assert {row.id for row in found} == {"10", "11"}
    assert len(found) == 2


def test_identical_duplicate_is_canonicalized_and_order_independent(tmp_path):
    product = item(21, article="000021", units_list=["шт", {"opaque": Decimal("1.25")}])
    path_a = tmp_path / "a.sqlite3"
    path_b = tmp_path / "b.sqlite3"
    mirror_a = TstnCatalogMirror(path_a)
    mirror_b = TstnCatalogMirror(path_b)

    first = sync(mirror_a, paged_client([product, product]), page_size=2)
    second = sync(mirror_b, paged_client([product, product]), page_size=2)

    assert first.duplicate_count == 1
    assert first.item_count == 1
    assert first.revision == second.revision
    stored = mirror_a.search(article="000021")[0]
    assert stored.units_list == ("шт", {"opaque": Decimal("1.25")})


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_duplicate_identity_fails_closed_independent_of_input_order(tmp_path, reverse):
    original = item(31, article="000031", name="Старый товар")
    replacement = item(31, article="000031", name="Другой товар")
    rows = [replacement, original] if reverse else [original, replacement]
    mirror = TstnCatalogMirror(tmp_path / "tstn.sqlite3")
    previous = sync(mirror, paged_client([item(1, article="000001", name="Previous")]), page_size=2)

    with pytest.raises(SourcingProviderError) as caught:
        sync(mirror, paged_client(rows), page_size=2)

    assert caught.value.code == "TSTN_SNAPSHOT_INVALID"
    assert mirror.revision == previous.revision
    assert mirror.search(article="000001")[0].name == "Previous"
    assert mirror.search(article="000031") == []


def test_article_fallback_identity_does_not_collide_with_numeric_item_id(tmp_path):
    mirror = TstnCatalogMirror(tmp_path / "tstn.sqlite3")
    rows = [
        item(77, article="000077", name="ID record"),
        item(None, article="77", name="Article-only record"),
    ]
    sync(mirror, paged_client(rows), page_size=2)

    assert mirror.count() == 2
    assert {row.identity_kind for row in mirror.search(article="000077")} == {"id"}
    assert mirror.search(item_id="77")[0].identity_kind == "id"


def test_invalid_identity_rejects_snapshot_instead_of_choosing_name_as_identity(tmp_path):
    mirror = TstnCatalogMirror(tmp_path / "tstn.sqlite3")

    with pytest.raises(SourcingProviderError):
        sync(mirror, paged_client([item(None, article=None)]), page_size=2)

    assert mirror.count() == 0
    assert mirror.revision == "unknown"


def test_interrupted_fetch_preserves_last_complete_revision_after_restart(tmp_path):
    path = tmp_path / "tstn.sqlite3"
    mirror = TstnCatalogMirror(path)
    original_client = paged_client([item(1, article="000001", name="Old snapshot")])
    original = sync(mirror, original_client, page_size=2)
    broken = FakeTstnClient(
        {
            0: page(
                [item(2, article="000002", name="Partial A"), item(3, article="000003", name="Partial B")],
                offset=0,
                limit=2,
                count_all=3,
            ),
        },
        failure_by_offset={2: RuntimeError(f"upstream URL leaked {SYNTHETIC_TOKEN}")},
    )

    with pytest.raises(SourcingProviderError) as caught:
        sync(mirror, broken, page_size=2)
    reopened = TstnCatalogMirror(path)

    assert SYNTHETIC_TOKEN not in str(caught.value)
    assert reopened.revision == original.revision
    assert [row.name for row in reopened.search(article="000001")] == ["Old snapshot"]
    assert reopened.search(article="000002") == []


def test_short_snapshot_without_count_all_is_rejected_and_keeps_previous_revision(tmp_path):
    path = tmp_path / "no-count.sqlite3"
    mirror = TstnCatalogMirror(path)
    original = sync(mirror, paged_client([item(1, article="000001", name="Old")]))
    truncated = FakeTstnClient({
        0: page(
            [item(2, article="000002", name="Partial")],
            offset=0,
            limit=2,
            count_all=None,
        ),
    })

    with pytest.raises(SourcingProviderError) as caught:
        sync(mirror, truncated)

    assert caught.value.code == "TSTN_SNAPSHOT_INVALID"
    assert mirror.revision == original.revision
    assert mirror.search(article="000001")[0].name == "Old"
    assert mirror.search(article="000002") == []


def test_sqlite_write_interruption_rolls_back_replacement(tmp_path, monkeypatch):
    path = tmp_path / "tstn.sqlite3"
    mirror = TstnCatalogMirror(path)
    old = sync(mirror, paged_client([item(1, article="000001", name="Old")]), page_size=2)
    real_insert = mirror._insert_snapshot

    def interrupt_after_first_record(connection, records):
        first = {next(iter(records)): next(iter(records.values()))}
        real_insert(connection, first)
        raise RuntimeError("synthetic local interruption")

    monkeypatch.setattr(mirror, "_insert_snapshot", interrupt_after_first_record)
    with pytest.raises(SourcingProviderError) as caught:
        sync(mirror, paged_client([item(2, article="000002", name="New")]), page_size=2)
    monkeypatch.setattr(mirror, "_insert_snapshot", real_insert)

    reopened = TstnCatalogMirror(path)
    assert caught.value.code == "TSTN_MIRROR_UPDATE_FAILED"
    assert reopened.revision == old.revision
    assert [row.name for row in reopened.search(article="000001")] == ["Old"]
    assert reopened.search(article="000002") == []


@pytest.mark.parametrize(
    "bad_page",
    [
        "short_incomplete",
        "count_below_page_size",
        "wrong_page_number",
        "count_all_below_offset",
        "count_all_changed",
        "count_all_exceeds_traversal",
    ],
)
def test_incomplete_or_contradictory_page_metadata_preserves_old_catalog(tmp_path, bad_page):
    mirror = TstnCatalogMirror(tmp_path / "tstn.sqlite3")
    original = sync(mirror, paged_client([item(1, article="000001", name="Old")]), page_size=2)
    first = page(
        [item(2, article="000002"), item(3, article="000003")],
        offset=0,
        limit=2,
        count_all=4,
    )
    if bad_page == "short_incomplete":
        second = page([item(4, article="000004")], offset=2, limit=2, count_all=4)
        # The final page reports a total that would require one more record.
        second = page([item(4, article="000004")], offset=2, limit=2, count_all=5)
    elif bad_page == "count_below_page_size":
        second = page([item(4, article="000004")], offset=2, limit=2, count_all=3, count=0)
    elif bad_page == "wrong_page_number":
        second = page([item(4, article="000004")], offset=2, limit=2, count_all=3, page_num=9)
    elif bad_page == "count_all_below_offset":
        second = page([item(4, article="000004")], offset=2, limit=2, count_all=2)
    elif bad_page == "count_all_changed":
        second = page([item(4, article="000004")], offset=2, limit=2, count_all=3)
    else:
        second = page([item(4, article="000004")], offset=2, limit=2, count_all=4)
    client = FakeTstnClient({0: first, 2: second})

    with pytest.raises(SourcingProviderError):
        sync(mirror, client, page_size=2)

    assert mirror.revision == original.revision
    assert mirror.search(article="000001")[0].name == "Old"


def test_repeated_page_is_rejected_even_when_count_all_looks_stable(tmp_path):
    duplicated_page_rows = [item(10, article="000010"), item(11, article="000011")]
    repeated = page(duplicated_page_rows, offset=0, limit=2, count_all=4)
    mirror = TstnCatalogMirror(tmp_path / "tstn.sqlite3")
    client = FakeTstnClient({
        0: repeated,
        2: page(duplicated_page_rows, offset=2, limit=2, count_all=4),
    })

    with pytest.raises(SourcingProviderError):
        sync(mirror, client, page_size=2)

    assert mirror.count() == 0


def test_page_limit_and_item_limit_are_hard_bounded(tmp_path):
    mirror = TstnCatalogMirror(tmp_path / "tstn.sqlite3")
    client = paged_client([item(1, article="000001")])

    with pytest.raises(SourcingProviderError) as too_many_pages:
        sync(mirror, client, max_pages=TSTN_MAX_SYNC_PAGES + 1)
    assert too_many_pages.value.code == "TSTN_SYNC_LIMIT_INVALID"
    with pytest.raises(SourcingProviderError) as too_many_items:
        sync(mirror, client, page_size=2, max_pages=3, max_items=5)
    assert too_many_items.value.code == "TSTN_SYNC_LIMIT_INVALID"


def test_exceeding_page_or_snapshot_limits_keeps_previous_catalog(tmp_path):
    mirror = TstnCatalogMirror(tmp_path / "tstn.sqlite3")
    previous = sync(mirror, paged_client([item(1, article="000001", name="Old")]), page_size=2)
    rows = [item(2, article="000002", name="A"), item(3, article="000003", name="B")]
    too_many = FakeTstnClient({
        0: page(rows, offset=0, limit=2, count_all=4),
        2: page(rows, offset=2, limit=2, count_all=4),
    })

    with pytest.raises(SourcingProviderError) as caught:
        sync(mirror, too_many, page_size=2, max_snapshot_bytes=50)

    assert caught.value.code == "TSTN_SNAPSHOT_LIMIT"
    assert mirror.revision == previous.revision
    assert mirror.search(article="000001")[0].name == "Old"


def test_actual_item_cap_is_enforced_without_publishing_partial_snapshot(tmp_path):
    mirror = TstnCatalogMirror(tmp_path / "tstn.sqlite3")
    previous = sync(mirror, paged_client([item(1, article="000001", name="Old")]), page_size=2)
    client = FakeTstnClient({
        0: page([item(2, article="000002"), item(3, article="000003")], offset=0, limit=2, count_all=3),
        2: page([item(4, article="000004")], offset=2, limit=2, count_all=3),
    })

    with pytest.raises(SourcingProviderError) as caught:
        sync(mirror, client, page_size=2, max_pages=3, max_items=2)

    assert caught.value.code == "TSTN_SNAPSHOT_LIMIT"
    assert mirror.revision == previous.revision
    assert mirror.search(article="000001")[0].name == "Old"


def test_search_and_stats_are_local_and_do_not_call_client_or_store_credentials(tmp_path, monkeypatch):
    client = paged_client([item(1, article="000001", name="Локальный товар")])
    mirror_path = tmp_path / "tstn.sqlite3"
    mirror = TstnCatalogMirror(mirror_path)
    sync(mirror, client, page_size=2)
    request_count = len(client.calls)

    full_checks = 0
    original_check = mirror.check_integrity

    def counted_check():
        nonlocal full_checks
        full_checks += 1
        return original_check()

    monkeypatch.setattr(mirror, "check_integrity", counted_check)
    assert mirror.stats()["integrity_ok"] is True
    assert full_checks == 0
    for _ in range(20):
        assert mirror.search(name="локальный товар")
    assert full_checks == 0

    reopened = TstnCatalogMirror(mirror_path)
    reopened_checks = 0
    reopened_original_check = reopened.check_integrity

    def counted_reopened_check():
        nonlocal reopened_checks
        reopened_checks += 1
        return reopened_original_check()

    monkeypatch.setattr(reopened, "check_integrity", counted_reopened_check)
    assert reopened.stats()["integrity_ok"] is None
    assert reopened_checks == 0
    assert reopened.search(article="000001")
    assert reopened_checks == 1
    for _ in range(20):
        assert reopened.search(article="000001")
    assert reopened_checks == 1
    assert len(client.calls) == request_count
    assert SYNTHETIC_TOKEN.encode("utf-8") not in mirror_path.read_bytes()
    with sqlite3.connect(mirror_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(tstn_catalog_products)")}
    assert not columns.intersection({"price", "currency", "availability", "stock", "vat"})


@pytest.mark.parametrize("corruption", ["record", "index"])
def test_corrupt_record_or_index_is_detected_on_reopen_and_search_fails_closed(tmp_path, corruption):
    path = tmp_path / "tstn.sqlite3"
    mirror = TstnCatalogMirror(path)
    sync(mirror, paged_client([item(1, article="000001")]), page_size=2)
    with sqlite3.connect(path) as connection:
        if corruption == "record":
            connection.execute(
                "UPDATE tstn_catalog_products SET name='altered' WHERE identity_value='1'"
            )
        else:
            connection.execute(
                "DELETE FROM tstn_search_terms WHERE identity_value='1' AND term='гибкая'"
            )
    reopened = TstnCatalogMirror(path)

    with pytest.raises(SourcingProviderError) as caught:
        reopened.search(article="000001")
    assert caught.value.code == "TSTN_CATALOG_NOT_READY"
    assert reopened.stats()["integrity_ok"] is False


def test_index_can_be_rebuilt_locally_without_supplier_traffic(tmp_path):
    path = tmp_path / "tstn.sqlite3"
    mirror = TstnCatalogMirror(path)
    client = paged_client([item(1, article="000001", name="Локальный поиск")])
    result = sync(mirror, client, page_size=2)
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM tstn_search_terms")
    assert mirror.check_integrity() is False

    revision = mirror.rebuild_search_index()

    assert revision == result.search_index_revision
    assert mirror.check_integrity() is True
    assert mirror.search(name="локальный поиск")
    assert len(client.calls) == 1


def test_product_intent_is_supported_as_retrieval_input_not_match_decision(tmp_path):
    mirror = TstnCatalogMirror(tmp_path / "tstn.sqlite3")
    sync(mirror, paged_client([item(1, article="000001", name="Черепица Джаз")]), page_size=2)
    intent = ProductIntent(
        source_row_id="row-1",
        source_text="черепица джаз",
        normalized_name="черепица джаз",
        article="000001",
        search_queries=["гибкая черепица"],
    )

    candidates = mirror.search(intent)

    assert candidates
    assert not hasattr(candidates[0], "price")
    assert not hasattr(candidates[0], "decision")
