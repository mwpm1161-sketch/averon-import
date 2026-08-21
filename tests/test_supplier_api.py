from decimal import Decimal

from fastapi.testclient import TestClient

from averon_import.application import create_app
from averon_import.suppliers.models import ProductOffer
from averon_import.suppliers.registry import SupplierRegistry
from averon_import.suppliers.service import SupplierSearchService


class DemoAdapter:
    supplier_id = "demo"

    def search(self, query):
        return [ProductOffer(
            supplier="demo",
            title="Клапан Danfoss VFG2 DN50",
            url="https://example.test/item",
            manufacturer="Danfoss",
            model="VFG2 DN50",
            article="065B2403",
            price=Decimal("12345.67"),
            availability="В наличии",
        )]


def test_supplier_api_uses_registered_adapter_without_network(tmp_path):
    app = create_app(tmp_path / "data")
    services = app.state.services
    services.suppliers = SupplierSearchService(SupplierRegistry([DemoAdapter()]))

    source = tmp_path / "source.pdf"
    source.write_bytes(b"placeholder")
    workspace = services.workspace.create(
        source,
        {"filename": "source.pdf", "page_count": 1, "size": 11},
    )
    services.workspace.write_result(workspace, {
        "pages": [1],
        "rows": [{
            "id": "row-1", "name": "Клапан регулирующий", "manufacturer": "Danfoss",
            "type_mark": "VFG2 DN50", "code": "065B2403", "page": 1,
            "confidence": 90, "status": "recognized", "row_type": "item",
        }],
        "page_tables": {}, "errors": [], "summary": {}, "ocr_mode": "standard",
    })

    client = TestClient(app)
    assert client.get("/api/suppliers").json() == {"suppliers": ["demo"]}
    response = client.post(
        f"/api/documents/{workspace.document_id}/rows/row-1/supplier-search",
        json={"supplier_ids": ["demo"]},
    )
    assert response.status_code == 200
    matches = response.json()["matches"]
    assert len(matches) == 1
    assert matches[0]["score"] == 100.0
    assert matches[0]["offer"]["price"] == "12345.67"
