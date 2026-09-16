from __future__ import annotations

from datetime import datetime, timezone
import pytest

from averon_import.services.sourcing.models import ProductIntent
from averon_import.services.sourcing.providers.base import SourcingProviderError
from averon_import.services.sourcing.providers.lemana_b2b import (
    LemanaCatalogMirror,
    LemanaMirrorSyncResult,
    LemanaProductRecord,
    LemanaProductsPage,
)


def product(
    item: str,
    *,
    name: str = "Клапан шаровый",
    model: str = "K-100",
    brand: str = "Brand",
    available: bool | None = True,
    price_hint: str = "",
) -> LemanaProductRecord:
    return LemanaProductRecord(
        product_item=item,
        product_available=available,
        product_name=name,
        product_description=f"Описание {item}",
        product_url=f"https://example.test/products/{item}",
        product_model=model,
        product_brand=brand,
        product_photo=[f"https://example.test/{item}.jpg"],
        product_barcode=f"460{item}",
        product_params={"diameter": "100", "hint": price_hint},
        product_unit_sale={"name": "шт."},
        categories=[{"id": 10}],
    )


class FakeMirrorClient:
    def __init__(self, pages=(), failure: Exception | None = None):
        self.pages = list(pages)
        self.failure = failure
        self.calls: list[dict] = []

    def get_products(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        if not self.pages:
            raise AssertionError("unexpected page request")
        return self.pages.pop(0)


def page(items, *, page_number=1, per_page=100, total_count=None, malformed=0):
    return LemanaProductsPage(
        products=tuple(items),
        page=page_number,
        per_page=per_page,
        total_count=total_count,
        malformed_count=malformed,
    )


def intent(*, article: str = "", model: str = "", query: str = "клапан") -> ProductIntent:
    return ProductIntent(
        source_row_id="row-1",
        source_text=query,
        normalized_name=query,
        article=article,
        model=model,
        search_queries=[query],
    )


def test_one_page_sync_maps_supplier_facts_and_creates_stable_revision(tmp_path):
    mirror = LemanaCatalogMirror(tmp_path / "lemana" / "catalog.sqlite3")
    fixed_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    client = FakeMirrorClient([page([product("82331508")], total_count=1)])

    result = mirror.sync(client, region_id=34, now=fixed_now)

    assert isinstance(result, LemanaMirrorSyncResult)
    assert result.changed is True
    assert result.item_count == 1
    assert result.revision == mirror.revision
    assert mirror.count() == 1
    assert mirror.region_id == 34
    stored = mirror.search(intent(article="82331508"))[0]
    assert stored.product_item == "82331508"
    assert stored.product_model == "K-100"
    assert stored.product_brand == "Brand"
    assert stored.product_url.endswith("82331508")
    assert stored.product_params["diameter"] == "100"
    assert stored.categories == [{"id": 10}]


def test_multi_page_sync_deduplicates_by_product_item_and_is_incremental(tmp_path):
    mirror = LemanaCatalogMirror(tmp_path / "catalog.sqlite3")
    client = FakeMirrorClient(
        [
            page([product("1"), product("2")], page_number=1, per_page=2, total_count=3),
            page([product("2", name="Updated duplicate"), product("3")], page_number=2, per_page=2, total_count=3),
        ]
    )

    first = mirror.sync(client, region_id=34, per_page=2)
    revision = first.revision
    same_client = FakeMirrorClient(
        [
            # A 200 response to the If-Modified-Since probe is a delta, not
            # a snapshot.  The following response is the complete snapshot.
            page([product("2", name="Updated duplicate")], total_count=1),
            page(
                [product("1"), product("2", name="Updated duplicate"), product("3")],
                total_count=3,
            ),
        ]
    )
    second = mirror.sync(
        same_client,
        region_id=34,
        now=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    assert first.item_count == 3
    assert mirror.count() == 3
    assert mirror.search(intent(query="Updated duplicate"))[0].product_name == "Updated duplicate"
    assert second.changed is False
    assert second.revision == revision
    assert mirror.last_synced_at.endswith("Z")
    assert same_client.calls[0]["if_modified_since"] is not None
    assert same_client.calls[1]["if_modified_since"] is None


def test_not_modified_does_not_change_revision_or_content(tmp_path):
    mirror = LemanaCatalogMirror(tmp_path / "catalog.sqlite3")
    mirror.sync(FakeMirrorClient([page([product("1")], total_count=1)]), region_id=34)
    revision = mirror.revision
    timestamp = mirror.last_synced_at

    result = mirror.sync(FakeMirrorClient([None]), region_id=34)

    assert result.not_modified is True
    assert result.changed is False
    assert result.revision == revision
    assert mirror.last_synced_at == timestamp
    assert mirror.count() == 1


def test_failed_multi_page_sync_preserves_previous_mirror(tmp_path):
    mirror = LemanaCatalogMirror(tmp_path / "catalog.sqlite3")
    mirror.sync(FakeMirrorClient([page([product("old")], total_count=1)]), region_id=34)
    old_revision = mirror.revision

    broken = FakeMirrorClient(
        [page([product("new")], page_number=1, per_page=1, total_count=2)],
        failure=SourcingProviderError("Поставщик временно недоступен", category="network"),
    )
    with pytest.raises(SourcingProviderError):
        mirror.sync(broken, region_id=34, per_page=1)

    assert mirror.revision == old_revision
    assert mirror.count() == 1
    assert mirror.search(intent(article="old"))[0].product_item == "old"


def test_failed_full_snapshot_after_delta_probe_preserves_previous_mirror(tmp_path):
    mirror = LemanaCatalogMirror(tmp_path / "catalog.sqlite3")
    mirror.sync(FakeMirrorClient([page([product("old")], total_count=1)]), region_id=34)
    old_revision = mirror.revision

    class FailAfterProbeClient:
        def __init__(self):
            self.calls: list[dict] = []

        def get_products(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return page([product("new")], total_count=1)
            raise SourcingProviderError("temporary upstream failure", category="network")

    broken = FailAfterProbeClient()
    with pytest.raises(SourcingProviderError):
        mirror.sync(broken, region_id=34)

    assert broken.calls[0]["if_modified_since"] is not None
    assert broken.calls[1]["if_modified_since"] is None
    assert mirror.revision == old_revision
    assert mirror.count() == 1
    assert mirror.search(intent(article="old"))[0].product_item == "old"


def test_malformed_rows_are_isolated_when_valid_rows_remain(tmp_path):
    mirror = LemanaCatalogMirror(tmp_path / "catalog.sqlite3")
    client = FakeMirrorClient([page([product("valid")], total_count=1, malformed=2)])

    result = mirror.sync(client, region_id=34)

    assert result.item_count == 1
    assert result.malformed_count == 2
    assert mirror.count() == 1


def test_local_retrieval_prioritizes_exact_item_model_and_is_deterministic(tmp_path):
    mirror = LemanaCatalogMirror(tmp_path / "catalog.sqlite3")
    mirror.sync(
        FakeMirrorClient(
            [
                page(
                    [
                        product("100", name="Муфта", model="M-1"),
                        product("82331508", name="Клапан точный", model="K-100"),
                        product("200", name="Клапан похожий", model="K-100"),
                    ],
                    total_count=3,
                )
            ]
        ),
        region_id=34,
    )

    exact = mirror.search(intent(article="82331508", query="клапан"), limit=3)
    model = mirror.search(intent(model="K-100", query="клапан"), limit=3)
    text_first = mirror.search(intent(query="муфта"), limit=3)
    text_second = mirror.search(intent(query="муфта"), limit=3)

    assert exact[0].product_item == "82331508"
    assert {item.product_item for item in model[:2]} == {"82331508", "200"}
    assert text_first[0].product_item == "100"
    assert [item.product_item for item in text_first] == [item.product_item for item in text_second]


def test_revision_changes_only_after_accepted_content_update(tmp_path):
    mirror = LemanaCatalogMirror(tmp_path / "catalog.sqlite3")
    mirror.sync(FakeMirrorClient([page([product("1", price_hint="a")], total_count=1)]), region_id=34)
    original = mirror.revision

    updated = mirror.sync(
        FakeMirrorClient(
            [
                page([product("1", price_hint="b")], total_count=1),
                page([product("1", price_hint="b")], total_count=1),
            ]
        ),
        region_id=34,
    )

    assert updated.changed is True
    assert updated.revision != original
    assert mirror.search(intent(article="1"))[0].product_params["hint"] == "b"


def test_region_switch_does_not_probe_or_reuse_old_mirror(tmp_path):
    mirror = LemanaCatalogMirror(tmp_path / "catalog.sqlite3")
    mirror.sync(FakeMirrorClient([page([product("old")], total_count=1)]), region_id=34)

    switched = FakeMirrorClient([page([product("new")], total_count=1)])
    result = mirror.sync(switched, region_id=35)

    assert result.changed is True
    assert switched.calls[0]["if_modified_since"] is None
    assert mirror.region_id == 35
    assert "old" not in {item.product_item for item in mirror.search(intent(article="old"))}
    assert mirror.search(intent(article="new"))[0].product_item == "new"


def test_environment_switch_does_not_probe_or_reuse_old_mirror(tmp_path):
    mirror = LemanaCatalogMirror(tmp_path / "catalog.sqlite3")
    mirror.sync(
        FakeMirrorClient([page([product("test")], total_count=1)]),
        region_id=34,
        environment="test",
    )

    switched = FakeMirrorClient([page([product("prod")], total_count=1)])
    result = mirror.sync(switched, region_id=34, environment="prod")

    assert result.changed is True
    assert switched.calls[0]["if_modified_since"] is None
    assert mirror.environment == "prod"
    assert "test" not in {item.product_item for item in mirror.search(intent(article="test"))}
    assert mirror.search(intent(article="prod"))[0].product_item == "prod"


def test_revision_includes_region_and_environment(tmp_path):
    mirror = LemanaCatalogMirror(tmp_path / "catalog.sqlite3")
    first = mirror.sync(
        FakeMirrorClient([page([product("same")], total_count=1)]),
        region_id=34,
        environment="test",
    )
    second = mirror.sync(
        FakeMirrorClient([page([product("same")], total_count=1)]),
        region_id=35,
        environment="prod",
    )

    assert second.revision != first.revision
    assert mirror.region_id == 35
    assert mirror.environment == "prod"


def test_exact_item_is_retrieved_before_broad_candidate_limit(tmp_path):
    mirror = LemanaCatalogMirror(tmp_path / "catalog.sqlite3")
    products = [product(f"{index:06d}", name="Клапан массовый") for index in range(1001)]
    products.append(product("999999", name="Клапан точный"))
    pages = [
        page(chunk, page_number=page_number, per_page=100, total_count=len(products))
        for page_number, chunk in enumerate(
            (products[index : index + 100] for index in range(0, len(products), 100)),
            start=1,
        )
    ]
    mirror.sync(FakeMirrorClient(pages), region_id=34, per_page=100)

    first = mirror.search(intent(article="999999", query="клапан"), limit=1)
    second = mirror.search(intent(article="999999", query="клапан"), limit=1)

    assert first[0].product_item == "999999"
    assert [item.product_item for item in first] == [item.product_item for item in second]
