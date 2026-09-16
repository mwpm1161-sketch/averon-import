from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from averon_import.services.sourcing.catalog_repository import normalize_catalog_text
from averon_import.services.sourcing.models import ProductIntent
from averon_import.services.sourcing.providers.base import SourcingProviderError

from .models import LemanaProductRecord, LemanaProductsPage

_DEFAULT_PAGE_SIZE = 100
_MAX_PAGES = 1_000
_MAX_ITEMS = 100_000


@dataclass(frozen=True)
class LemanaMirrorSyncResult:
    changed: bool
    not_modified: bool
    item_count: int
    revision: str
    malformed_count: int
    synced_at: str


class LemanaCatalogMirror:
    """Provider-owned local catalog facts and deterministic candidate retrieval."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS lemana_mirror_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO lemana_mirror_meta(key, value) VALUES ('revision', '');
                INSERT OR IGNORE INTO lemana_mirror_meta(key, value) VALUES ('last_synced_at', '');
                INSERT OR IGNORE INTO lemana_mirror_meta(key, value) VALUES ('if_modified_since', '');
                INSERT OR IGNORE INTO lemana_mirror_meta(key, value) VALUES ('region_id', '');
                CREATE TABLE IF NOT EXISTS lemana_products (
                    product_item TEXT PRIMARY KEY,
                    product_available INTEGER,
                    product_name TEXT NOT NULL,
                    product_description TEXT NOT NULL,
                    product_url TEXT NOT NULL,
                    product_model TEXT NOT NULL,
                    product_brand TEXT NOT NULL,
                    product_photo_json TEXT,
                    product_barcode TEXT NOT NULL,
                    product_params_json TEXT NOT NULL,
                    product_unit_sale_json TEXT,
                    categories_json TEXT NOT NULL,
                    normalized_text TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_lemana_products_text
                    ON lemana_products(normalized_text);
                CREATE INDEX IF NOT EXISTS idx_lemana_products_model
                    ON lemana_products(product_model);
                """
            )

    @property
    def revision(self) -> str:
        return self._meta("revision") or "unknown"

    @property
    def last_synced_at(self) -> str:
        return self._meta("last_synced_at")

    @property
    def if_modified_since(self) -> str:
        return self._meta("if_modified_since")

    @property
    def region_id(self) -> int | None:
        value = self._meta("region_id")
        try:
            return int(value) if value else None
        except ValueError:
            return None

    def _meta(self, key: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM lemana_mirror_meta WHERE key=?", (key,)
            ).fetchone()
        return str(row[0]) if row else ""

    def count(self) -> int:
        with self._connect() as connection:
            return int(
                connection.execute("SELECT COUNT(*) FROM lemana_products").fetchone()[0]
            )

    def has_content(self) -> bool:
        return self.count() > 0

    def stats(self) -> dict[str, Any]:
        return {
            "item_count": self.count(),
            "catalog_version": self.revision,
            "last_synced_at": self.last_synced_at,
            "region_id": self.region_id,
        }

    def sync(
        self,
        client: Any,
        *,
        region_id: int,
        per_page: int = _DEFAULT_PAGE_SIZE,
        max_pages: int = _MAX_PAGES,
        max_items: int = _MAX_ITEMS,
        if_modified_since: str | None = None,
        now: datetime | None = None,
    ) -> LemanaMirrorSyncResult:
        if isinstance(region_id, bool) or not isinstance(region_id, int) or region_id < 1:
            raise SourcingProviderError(
                "Для зеркала Lemana PRO B2B не указан регион",
                code="INVALID_REQUEST",
                category="invalid_request",
            )
        page_size = max(1, min(int(per_page), _DEFAULT_PAGE_SIZE))
        page_limit = max(1, min(int(max_pages), _MAX_PAGES))
        item_limit = max(1, min(int(max_items), _MAX_ITEMS))
        request_if_modified_since = (
            if_modified_since if if_modified_since is not None else self.if_modified_since
        ) or None
        collected: dict[str, LemanaProductRecord] = {}
        malformed_count = 0
        total_count: int | None = None
        not_modified = False
        for page_number in range(1, page_limit + 1):
            try:
                page = client.get_products(
                    region_id=region_id,
                    page=page_number,
                    per_page=page_size,
                    if_modified_since=request_if_modified_since,
                )
            except SourcingProviderError:
                raise
            except Exception as exc:
                raise SourcingProviderError(
                    "Синхронизация каталога Lemana PRO B2B не выполнена",
                    category="upstream_error",
                ) from exc
            if page is None:
                if page_number == 1:
                    not_modified = True
                    break
                raise SourcingProviderError(
                    "Лемана PRO B2B вернула неполную синхронизацию каталога",
                    category="invalid_response",
                )
            if not isinstance(page, LemanaProductsPage):
                raise SourcingProviderError(
                    "Лемана PRO B2B вернула некорректную страницу каталога",
                    category="invalid_response",
                )
            malformed_count += max(0, int(page.malformed_count))
            total_count = page.total_count if page.total_count is not None else total_count
            for product in page.products:
                collected[product.product_item] = product
                if len(collected) > item_limit:
                    raise SourcingProviderError(
                        "Каталог Lemana PRO B2B превышает безопасный лимит",
                        category="invalid_response",
                    )
            if len(page.products) < page_size or (
                total_count is not None and len(collected) >= total_count
            ):
                break
        else:
            raise SourcingProviderError(
                "Каталог Lemana PRO B2B превышает безопасный лимит страниц",
                category="invalid_response",
            )

        timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        if not_modified:
            return LemanaMirrorSyncResult(
                changed=False,
                not_modified=True,
                item_count=self.count(),
                revision=self.revision,
                malformed_count=0,
                synced_at=self.last_synced_at,
            )
        revision = self._fingerprint(collected, region_id)
        changed = revision != self.revision or self.region_id != region_id
        with self._connect() as connection:
            if changed:
                connection.execute("DELETE FROM lemana_products")
                for product in sorted(collected.values(), key=lambda item: item.product_item):
                    connection.execute(
                        """INSERT INTO lemana_products(
                           product_item, product_available, product_name,
                           product_description, product_url, product_model,
                           product_brand, product_photo_json, product_barcode,
                           product_params_json, product_unit_sale_json,
                           categories_json, normalized_text
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        self._record(product),
                    )
            self._set_meta(connection, "revision", revision)
            self._set_meta(connection, "last_synced_at", timestamp)
            self._set_meta(connection, "if_modified_since", timestamp)
            self._set_meta(connection, "region_id", str(region_id))
        return LemanaMirrorSyncResult(
            changed=changed,
            not_modified=False,
            item_count=len(collected),
            revision=revision,
            malformed_count=malformed_count,
            synced_at=timestamp,
        )

    @staticmethod
    def _set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
        connection.execute(
            "INSERT INTO lemana_mirror_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    @staticmethod
    def _json(value: Any, default: str) -> str:
        if value is None:
            return default
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _record(cls, product: LemanaProductRecord) -> tuple[Any, ...]:
        normalized_text = normalize_catalog_text(
            " ".join(
                [
                    product.product_item,
                    product.product_name,
                    product.product_model,
                    product.product_brand,
                    product.product_barcode,
                    cls._json(product.product_params, "{}"),
                ]
            )
        )
        return (
            product.product_item,
            None if product.product_available is None else int(product.product_available),
            product.product_name,
            product.product_description,
            product.product_url,
            product.product_model,
            product.product_brand,
            cls._json(product.product_photo, "null"),
            product.product_barcode,
            cls._json(product.product_params, "{}"),
            cls._json(product.product_unit_sale, "null"),
            cls._json(product.categories, "[]"),
            normalized_text,
        )

    @classmethod
    def _fingerprint(
        cls, products: dict[str, LemanaProductRecord], region_id: int
    ) -> str:
        payload = {
            "region_id": region_id,
            "products": [
                json.loads(json.dumps(product.model_dump(mode="json"), ensure_ascii=False))
                for product in sorted(products.values(), key=lambda item: item.product_item)
            ],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def search(self, intent: ProductIntent, *, limit: int = 20) -> list[LemanaProductRecord]:
        terms: list[str] = []
        for value in [
            *intent.search_queries,
            intent.normalized_name,
            intent.model,
            intent.article,
            intent.manufacturer,
            intent.brand,
        ]:
            terms.extend(_tokens(value))
        for mapping in (intent.attributes, intent.required_attributes, intent.preferred_attributes):
            for key, value in mapping.items():
                terms.extend(_tokens(key))
                terms.extend(_tokens(value))
        terms = list(dict.fromkeys(terms))[:32]
        if not terms:
            return []
        clauses = " OR ".join("normalized_text LIKE ?" for _ in terms)
        params = [f"%{term}%" for term in terms]
        candidate_limit = min(max(max(1, int(limit)) * 10, 100), 1_000)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM lemana_products WHERE {clauses} "
                "ORDER BY product_item LIMIT ?",
                [*params, candidate_limit],
            ).fetchall()
        ranked = [(self._score(intent, row, terms), self._from_row(row)) for row in rows]
        ranked.sort(key=lambda item: (-item[0], item[1].product_item))
        return [product for _, product in ranked[: max(1, min(int(limit), 100))]]

    @staticmethod
    def _score(intent: ProductIntent, row: sqlite3.Row, terms: list[str]) -> int:
        article = normalize_catalog_text(intent.article)
        model = normalize_catalog_text(intent.model)
        item = normalize_catalog_text(row["product_item"])
        row_model = normalize_catalog_text(row["product_model"])
        score = 0
        if article and article == item:
            score += 1_000
        if model and model == row_model:
            score += 500
        text = row["normalized_text"]
        for term in terms:
            if term in text:
                score += 2
        return score

    @classmethod
    def _from_row(cls, row: sqlite3.Row) -> LemanaProductRecord:
        return LemanaProductRecord.model_validate(
            {
                "product_item": row["product_item"],
                "product_available": (
                    None if row["product_available"] is None else bool(row["product_available"])
                ),
                "product_name": row["product_name"],
                "product_description": row["product_description"],
                "product_url": row["product_url"],
                "product_model": row["product_model"],
                "product_brand": row["product_brand"],
                "product_photo": json.loads(row["product_photo_json"] or "null"),
                "product_barcode": row["product_barcode"],
                "product_params": json.loads(row["product_params_json"] or "{}"),
                "product_unit_sale": json.loads(row["product_unit_sale_json"] or "null"),
                "categories": json.loads(row["categories_json"] or "[]"),
            }
        )


def _tokens(value: Any) -> list[str]:
    return [token for token in normalize_catalog_text(value).split() if len(token) > 1]
