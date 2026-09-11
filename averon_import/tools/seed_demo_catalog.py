"""Seed the synthetic Averon demo catalog without clearing existing offers."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from averon_import.services.sourcing.catalog_repository import CatalogRepository
from averon_import.services.sourcing.demo_catalog import (
    DEMO_CATALOG_FIXTURE,
    seed_demo_catalog,
)


def default_catalog_database() -> Path:
    configured = os.environ.get("AVERON_DATA_DIR", "").strip()
    if configured:
        data_dir = Path(configured).expanduser().resolve()
    elif os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        root = Path(local) if local else Path.home() / "AppData" / "Local"
        data_dir = root / "Averon Import" / "data"
    else:
        data_dir = Path.cwd() / "data"
    return data_dir / "sourcing" / "catalog.sqlite3"


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the synthetic Averon demo catalog")
    parser.add_argument("--database", type=Path, default=default_catalog_database())
    parser.add_argument("--fixture", type=Path, default=DEMO_CATALOG_FIXTURE)
    args = parser.parse_args()
    result = seed_demo_catalog(CatalogRepository(args.database), args.fixture)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
