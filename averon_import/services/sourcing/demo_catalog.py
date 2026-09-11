"""Explicit, synthetic demo-catalog seeding on top of CatalogRepository."""

from __future__ import annotations

import json
from pathlib import Path

from averon_import.services.sourcing.catalog_repository import CatalogRepository

DEMO_CATALOG_SOURCE = "averon_demo_store"
DEMO_CATALOG_NOTICE = "Демонстрационный каталог Averon — тестовые данные"
DEMO_CATALOG_FIXTURE = (
    Path(__file__).resolve().parents[3] / "demo" / "catalog" / "demo_catalog.json"
)


def seed_demo_catalog(
    repository: CatalogRepository,
    fixture_path: Path | None = None,
) -> dict[str, object]:
    """Upsert only namespaced demo records without clearing other catalog data."""

    fixture = Path(fixture_path or DEMO_CATALOG_FIXTURE)
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError("Demo catalog fixture must contain a non-empty items list")
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Every demo catalog item must be an object")
        offer_id = str(item.get("offer_id") or item.get("id") or "")
        provenance = item.get("data_provenance") or {}
        if not offer_id.startswith("demo-"):
            raise ValueError("Demo catalog offer ids must use the demo- namespace")
        if provenance.get("source") != DEMO_CATALOG_SOURCE:
            raise ValueError("Demo catalog provenance must identify averon_demo_store")
        if item.get("url") != f"/demo-catalog/products/{offer_id}":
            raise ValueError("Demo catalog URL must resolve to its read-only product page")
    imported = repository.import_file(fixture, provider="local_catalog")
    demo_items = repository.list_by_source(DEMO_CATALOG_SOURCE, limit=500)
    return {
        "imported_count": imported,
        "demo_item_count": len(demo_items),
        "catalog_item_count": repository.count(),
        "catalog_version": repository.catalog_version,
        "source": DEMO_CATALOG_SOURCE,
    }
