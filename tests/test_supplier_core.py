from decimal import Decimal

from averon_import.suppliers.matcher import ProductMatcher
from averon_import.suppliers.models import ProductOffer, ProductQuery
from averon_import.suppliers.query_builder import build_product_query
from averon_import.suppliers.registry import SupplierRegistry
from averon_import.suppliers.service import SupplierSearchService


def test_query_builder_uses_existing_row_fields_only():
    query = build_product_query({
        "id": "r1",
        "name": "Клапан регулирующий",
        "manufacturer": "Danfoss",
        "type_mark": "VFG2 DN50",
        "code": "065B2403",
    })
    assert query.row_id == "r1"
    assert "Danfoss" in query.search_text
    assert "VFG2 DN50" in query.search_text
    assert "065B2403" in query.search_text
    assert query.attributes == {}


def test_exact_article_wins_when_manufacturer_is_compatible():
    query = ProductQuery(
        row_id="r1", search_text="x", name="Клапан", manufacturer="Danfoss",
        model="VFG2 DN50", article="065B2403",
    )
    offer = ProductOffer(
        supplier="demo", title="Клапан Danfoss VFG2 DN50", url="https://example.test/item",
        manufacturer="Danfoss", model="VFG2 DN50", article="065B2403",
        price=Decimal("12500.00"),
    )
    match = ProductMatcher().match(query, offer)
    assert match.score == 100
    assert match.exact_article is True
    assert match.hard_conflicts == []
    assert match.acceptable is True


def test_article_mismatch_is_a_hard_conflict():
    query = ProductQuery(row_id="r1", search_text="x", name="Клапан", article="ABC123")
    offer = ProductOffer(
        supplier="demo", title="Клапан", url="https://example.test/item", article="ZZZ999"
    )
    match = ProductMatcher().match(query, offer)
    assert "article_mismatch" in match.hard_conflicts
    assert match.score < 50
    assert match.acceptable is False


class DemoAdapter:
    supplier_id = "demo"

    def search(self, query):
        return [
            ProductOffer(
                supplier="demo",
                title="Клапан Danfoss VFG2 DN50",
                url="https://example.test/good",
                manufacturer="Danfoss",
                model="VFG2 DN50",
                article="065B2403",
                price=Decimal("10000"),
            ),
            ProductOffer(
                supplier="demo",
                title="Другой клапан",
                url="https://example.test/bad",
                manufacturer="Other",
                article="wrong",
                price=Decimal("5000"),
            ),
        ]


def test_supplier_service_returns_best_non_conflicting_match_first():
    registry = SupplierRegistry([DemoAdapter()])
    service = SupplierSearchService(registry)
    matches = service.search_row({
        "id": "r1",
        "name": "Клапан регулирующий",
        "manufacturer": "Danfoss",
        "type_mark": "VFG2 DN50",
        "code": "065B2403",
    })
    assert len(matches) == 2
    assert str(matches[0].offer.url).endswith("/good")
    assert matches[0].score == 100
    assert matches[1].hard_conflicts
