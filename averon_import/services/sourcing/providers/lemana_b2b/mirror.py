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
                INSERT OR IGNORE INTO lemana_mirror_meta(key, value) VALUES ('environment', '');
                CREATE TABLE IF NOT EXISTS lemana_products (
                    product_item TEXT PRIMARY KEY,
                    product_available INTEGER,
                    product_name TEXT NOT NULL,
                    product_description TEXT NOT NULL,
                    product_url TEXT NOT NULL,
                    product_model TEXT NOT NULL,
                    normalized_model TEXT NOT NULL DEFAULT '',
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
                """
            )
            existing = {row[1] for row in connection.execute("PRAGMA table_info(lemana_products)")}
            if "normalized_model" not in existing:
                connection.execute(
                    "ALTER TABLE lemana_products ADD COLUMN normalized_model TEXT NOT NULL DEFAULT ''"
                )
            rows = connection.execute(
                "SELECT product_item, product_model FROM lemana_products"
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE lemana_products SET normalized_model=? WHERE product_item=?",
                    (normalize_catalog_text(row["product_model"]), row["product_item"]),
                )
            connection.execute("DROP INDEX IF EXISTS idx_lemana_products_model")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_lemana_products_model "
                "ON lemana_products(normalized_model)"
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

    @property
    def environment(self) -> str:
        return self._meta("environment")

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
            "environment": self.environment,
        }

    def sync(
        self,
        client: Any,
        *,
        region_id: int,
        environment: str = "test",
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
        if environment not in {"test", "prod"}:
            raise SourcingProviderError(
                "Для зеркала Lemana PRO B2B указано неизвестное окружение",
                code="INVALID_REQUEST",
                category="invalid_request",
            )
        page_size = max(1, min(int(per_page), _DEFAULT_PAGE_SIZE))
        page_limit = max(1, min(int(max_pages), _MAX_PAGES))
        item_limit = max(1, min(int(max_items), _MAX_ITEMS))
        request_if_modified_since = (
            if_modified_since if if_modified_since is not None else self.if_modified_since
        ) or None
        snapshot_started = _as_utc(now or datetime.now(timezone.utc))
        safe_marker = _utc_marker(snapshot_started)
        can_probe = bool(
            self.has_content()
            and request_if_modified_since
            and self.region_id == region_id
            and self.environment == environment
        )
        if can_probe:
            probe = self._fetch_page(
                client,
                region_id=region_id,
                page_number=1,
                page_size=page_size,
                if_modified_since=request_if_modified_since,
            )
            if probe is None:
                # 304 is a no-op: neither content nor sync marker moves.
                return LemanaMirrorSyncResult(
                    changed=False,
                    not_modified=True,
                    item_count=self.count(),
                    revision=self.revision,
                    malformed_count=0,
                    synced_at=self.last_synced_at,
                )
            # A 200 probe is a delta response, not a snapshot.  Discard it and
            # restart page 1 without If-Modified-Since.
        collected, malformed_count = self._fetch_snapshot(
            client,
            region_id=region_id,
            page_size=page_size,
            page_limit=page_limit,
            item_limit=item_limit,
        )
        completed_at = _utc_marker(datetime.now(timezone.utc))
        revision = self._fingerprint(collected, region_id, environment)
        changed = (
            revision != self.revision
            or self.region_id != region_id
            or self.environment != environment
        )
        with self._connect() as connection:
            if changed:
                connection.execute("DELETE FROM lemana_products")
                for product in sorted(collected.values(), key=lambda item: item.product_item):
                    connection.execute(
                        """INSERT INTO lemana_products(
                           product_item, product_available, product_name,
                           product_description, product_url, product_model,
                           normalized_model, product_brand, product_photo_json,
                           product_barcode, product_params_json,
                           product_unit_sale_json, categories_json, normalized_text
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        self._record(product),
                    )
            self._set_meta(connection, "revision", revision)
            self._set_meta(connection, "last_synced_at", completed_at)
            # This marker is the beginning of the full snapshot, never its
            # completion time, so changes during a long sync are not skipped.
            self._set_meta(connection, "if_modified_since", safe_marker)
            self._set_meta(connection, "region_id", str(region_id))
            self._set_meta(connection, "environment", environment)
        return LemanaMirrorSyncResult(
            changed=changed,
            not_modified=False,
            item_count=len(collected),
            revision=revision,
            malformed_count=malformed_count,
            synced_at=completed_at,
        )

    def _fetch_page(
        self,
        client: Any,
        *,
        region_id: int,
        page_number: int,
        page_size: int,
        if_modified_since: str | None,
    ) -> LemanaProductsPage | None:
        try:
            page = client.get_products(
                region_id=region_id,
                page=page_number,
                per_page=page_size,
                if_modified_since=if_modified_since,
            )
        except SourcingProviderError:
            raise
        except Exception as exc:
            raise SourcingProviderError(
                "Синхронизация каталога Lemana PRO B2B не выполнена",
                category="upstream_error",
            ) from exc
        if page is not None and not isinstance(page, LemanaProductsPage):
            raise SourcingProviderError(
                "Лемана PRO B2B вернула некорректную страницу каталога",
                category="invalid_response",
            )
        return page

    def _fetch_snapshot(
        self,
        client: Any,
        *,
        region_id: int,
        page_size: int,
        page_limit: int,
        item_limit: int,
    ) -> tuple[dict[str, LemanaProductRecord], int]:
        collected: dict[str, LemanaProductRecord] = {}
        malformed_count = 0
        total_count: int | None = None
        for page_number in range(1, page_limit + 1):
            page = self._fetch_page(
                client,
                region_id=region_id,
                page_number=page_number,
                page_size=page_size,
                if_modified_since=None,
            )
            if page is None:
                raise SourcingProviderError(
                    "Лемана PRO B2B вернула неполную синхронизацию каталога",
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
                return collected, malformed_count
        raise SourcingProviderError(
            "Каталог Lemana PRO B2B превышает безопасный лимит страниц",
            category="invalid_response",
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
            normalize_catalog_text(product.product_model),
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
        cls, products: dict[str, LemanaProductRecord], region_id: int, environment: str
    ) -> str:
        payload = {
            "environment": environment,
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
        candidate_limit = min(max(max(1, int(limit)) * 10, 100), 1_000)
        rows_by_id: dict[str, sqlite3.Row] = {}
        with self._connect() as connection:
            article = str(intent.article or "").strip()
            if article:
                exact_rows = connection.execute(
                    "SELECT * FROM lemana_products WHERE product_item=?",
                    (article,),
                ).fetchall()
                rows_by_id.update({row["product_item"]: row for row in exact_rows})
            model = normalize_catalog_text(intent.model)
            if model:
                model_rows = connection.execute(
                    "SELECT * FROM lemana_products WHERE normalized_model=? "
                    "ORDER BY product_item LIMIT ?",
                    (model, candidate_limit),
                ).fetchall()
                rows_by_id.update({row["product_item"]: row for row in model_rows})
            clauses = " OR ".join("normalized_text LIKE ?" for _ in terms)
            params = [f"%{term}%" for term in terms]
            broad_rows = connection.execute(
                f"SELECT * FROM lemana_products WHERE {clauses} "
                "ORDER BY product_item LIMIT ?",
                [*params, candidate_limit],
            ).fetchall()
            rows_by_id.update({row["product_item"]: row for row in broad_rows})
        ranked = [
            (self._score(intent, row, terms), self._from_row(row))
            for row in rows_by_id.values()
        ]
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


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_marker(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")
