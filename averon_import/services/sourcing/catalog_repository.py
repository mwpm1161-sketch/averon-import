from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from averon_import.services.sourcing.models import Offer, ProductIntent


def normalize_catalog_text(value: object) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    text = text.replace("×", "x").replace("х", "x")
    text = text.replace("–", "-").replace("—", "-")
    text = text.replace("ду", "dn").replace("ру", "pn")
    text = re.sub(r"[^\w.+/-]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _tokens(value: object) -> list[str]:
    return [token for token in normalize_catalog_text(value).split() if len(token) > 1]


class CatalogRepository:
    """SQLite persistence for provider-owned commercial catalog facts."""

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
                CREATE TABLE IF NOT EXISTS catalog_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO catalog_meta(key, value) VALUES ('version', '0');
                CREATE TABLE IF NOT EXISTS catalog_items (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    normalized_title TEXT NOT NULL,
                    article TEXT NOT NULL DEFAULT '',
                    normalized_article TEXT NOT NULL DEFAULT '',
                    manufacturer TEXT NOT NULL DEFAULT '',
                    normalized_manufacturer TEXT NOT NULL DEFAULT '',
                    brand TEXT NOT NULL DEFAULT '',
                    normalized_brand TEXT NOT NULL DEFAULT '',
                    price NUMERIC,
                    currency TEXT NOT NULL DEFAULT '',
                    price_unit TEXT NOT NULL DEFAULT 'шт.',
                    availability INTEGER,
                    availability_text TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL DEFAULT '',
                    attributes_json TEXT NOT NULL DEFAULT '{}',
                    normalized_attributes TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL,
                    source_item_id TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_catalog_article ON catalog_items(article);
                CREATE INDEX IF NOT EXISTS idx_catalog_title ON catalog_items(normalized_title);
                CREATE INDEX IF NOT EXISTS idx_catalog_source ON catalog_items(source);
                """
            )
            existing = {row[1] for row in connection.execute("PRAGMA table_info(catalog_items)")}
            for name, definition in {
                "normalized_article": "TEXT NOT NULL DEFAULT ''",
                "normalized_manufacturer": "TEXT NOT NULL DEFAULT ''",
                "normalized_brand": "TEXT NOT NULL DEFAULT ''",
                "normalized_attributes": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in existing:
                    connection.execute(f"ALTER TABLE catalog_items ADD COLUMN {name} {definition}")
            # Migrate catalogs created by the initial pilot shape without
            # changing provider-owned commercial values.
            rows = connection.execute(
                "SELECT id, article, manufacturer, brand, attributes_json FROM catalog_items"
            ).fetchall()
            for row in rows:
                connection.execute(
                    """UPDATE catalog_items SET normalized_article=?,
                       normalized_manufacturer=?, normalized_brand=?,
                       normalized_attributes=? WHERE id=?""",
                    (
                        normalize_catalog_text(row["article"]),
                        normalize_catalog_text(row["manufacturer"]),
                        normalize_catalog_text(row["brand"]),
                        normalize_catalog_text(row["attributes_json"]),
                        row["id"],
                    ),
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_catalog_normalized_article ON catalog_items(normalized_article)"
            )

    @property
    def catalog_version(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM catalog_meta WHERE key='version'"
            ).fetchone()
        return str(row[0] if row else "0")

    def _bump_version(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            "UPDATE catalog_meta SET value=CAST(value AS INTEGER)+1 WHERE key='version'"
        )

    def upsert(self, offer: Offer) -> Offer:
        self.bulk_upsert([offer])
        return offer

    def bulk_upsert(self, offers: Iterable[Offer]) -> int:
        items = list(offers)
        if not items:
            return 0
        with self._connect() as connection:
            for offer in items:
                connection.execute(
                    """
                    INSERT INTO catalog_items(
                        id,title,normalized_title,article,normalized_article,manufacturer,
                        normalized_manufacturer,brand,normalized_brand,price,
                        currency,price_unit,availability,availability_text,url,
                        attributes_json,normalized_attributes,source,source_item_id,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        title=excluded.title,
                        normalized_title=excluded.normalized_title,
                        article=excluded.article,
                        normalized_article=excluded.normalized_article,
                        manufacturer=excluded.manufacturer,
                        normalized_manufacturer=excluded.normalized_manufacturer,
                        brand=excluded.brand,
                        normalized_brand=excluded.normalized_brand,
                        price=excluded.price,
                        currency=excluded.currency,
                        price_unit=excluded.price_unit,
                        availability=excluded.availability,
                        availability_text=excluded.availability_text,
                        url=excluded.url,
                        attributes_json=excluded.attributes_json,
                        normalized_attributes=excluded.normalized_attributes,
                        source=excluded.source,
                        source_item_id=excluded.source_item_id,
                        updated_at=excluded.updated_at
                    """,
                    self._record(offer),
                )
            self._bump_version(connection)
        return len(items)

    @staticmethod
    def _record(offer: Offer) -> tuple[Any, ...]:
        return (
            offer.offer_id,
            offer.title,
            normalize_catalog_text(offer.title),
            offer.article,
            normalize_catalog_text(offer.article),
            offer.manufacturer,
            normalize_catalog_text(offer.manufacturer),
            offer.brand,
            normalize_catalog_text(offer.brand),
            str(offer.price) if offer.price is not None else None,
            offer.currency,
            offer.price_unit,
            None if offer.availability is None else int(offer.availability),
            offer.availability_text,
            offer.url,
            json.dumps(offer.attributes, ensure_ascii=False, sort_keys=True),
            normalize_catalog_text(json.dumps(offer.attributes, ensure_ascii=False, sort_keys=True)),
            str(offer.data_provenance.get("source") or offer.provider),
            offer.source_item_id,
            offer.retrieved_at.isoformat(),
        )

    def get_by_id(self, offer_id: str) -> Offer | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM catalog_items WHERE id=?", (str(offer_id),)
            ).fetchone()
        return self._from_row(row) if row else None

    def list_by_source(self, source: str, *, limit: int = 100) -> list[Offer]:
        """Return provider-owned offers for a bounded read-only catalog view."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM catalog_items WHERE source=? "
                "ORDER BY normalized_title, id LIMIT ?",
                (str(source), max(1, min(int(limit), 500))),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM catalog_items").fetchone()[0])

    def stats(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count, COUNT(DISTINCT source) AS sources FROM catalog_items"
            ).fetchone()
        return {
            "item_count": int(row["count"] if row else 0),
            "source_count": int(row["sources"] if row else 0),
            "catalog_version": self.catalog_version,
        }

    def clear(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM catalog_items")
            self._bump_version(connection)

    def search(self, intent: ProductIntent, limit: int = 20) -> list[Offer]:
        terms: list[str] = []
        for value in [
            *intent.search_queries,
            intent.normalized_name,
            intent.model,
            intent.article,
            intent.manufacturer,
        ]:
            terms.extend(_tokens(value))
        for mapping in (intent.attributes, intent.required_attributes, intent.preferred_attributes):
            for key, value in mapping.items():
                terms.extend(_tokens(key))
                terms.extend(_tokens(value))
        terms = list(dict.fromkeys(terms))[:32]
        if not terms:
            return []
        clauses = []
        params: list[str] = []
        for token in terms:
            pattern = f"%{token}%"
            clauses.append(
                "(normalized_title LIKE ? OR normalized_article LIKE ? "
                "OR normalized_manufacturer LIKE ? OR normalized_brand LIKE ? "
                "OR normalized_attributes LIKE ?)"
            )
            params.extend([pattern] * 5)
        result_limit = max(1, min(int(limit), 100))
        candidate_limit = min(max(result_limit * 5, 100), 500)
        sql = (
            "SELECT * FROM catalog_items WHERE "
            + " OR ".join(clauses)
            + " ORDER BY updated_at DESC LIMIT ?"
        )
        params.append(str(candidate_limit))
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        ranked = []
        for row in rows:
            offer = self._from_row(row)
            score = self._score(intent, offer, terms)
            ranked.append((score, offer))
        ranked.sort(key=lambda item: (-item[0], item[1].offer_id))
        return [offer for _, offer in ranked[:result_limit]]

    @staticmethod
    def _score(intent: ProductIntent, offer: Offer, terms: list[str]) -> int:
        normalized_article = normalize_catalog_text(intent.article)
        article = normalize_catalog_text(offer.article)
        score = 100 if normalized_article and normalized_article == article else 0
        fields = {
            "title": normalize_catalog_text(offer.title),
            "article": article,
            "manufacturer": normalize_catalog_text(offer.manufacturer),
            "brand": normalize_catalog_text(offer.brand),
            "attributes": normalize_catalog_text(json.dumps(offer.attributes, ensure_ascii=False)),
        }
        for token in terms:
            if token in fields["title"]:
                score += 5
            if token in fields["article"]:
                score += 10
            if token in fields["manufacturer"] or token in fields["brand"]:
                score += 4
            if token in fields["attributes"]:
                score += 2
        return score

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Offer:
        return Offer(
            offer_id=row["id"],
            provider="local_catalog",
            source_item_id=row["source_item_id"],
            title=row["title"],
            article=row["article"],
            manufacturer=row["manufacturer"],
            brand=row["brand"],
            price=row["price"],
            currency=row["currency"],
            price_unit=row["price_unit"],
            availability=(None if row["availability"] is None else bool(row["availability"])),
            availability_text=row["availability_text"],
            url=row["url"],
            attributes=json.loads(row["attributes_json"] or "{}"),
            retrieved_at=row["updated_at"],
            data_provenance={"source": row["source"], "source_item_id": row["source_item_id"]},
        )

    def import_file(self, path: Path, *, provider: str = "local_catalog") -> int:
        path = Path(path)
        if path.suffix.casefold() == ".json":
            raw = json.loads(path.read_text(encoding="utf-8"))
            records = raw.get("items", raw) if isinstance(raw, dict) else raw
            if not isinstance(records, list):
                raise ValueError("JSON catalog must be a list or an object with items")
        elif path.suffix.casefold() == ".csv":
            with path.open("r", encoding="utf-8-sig", newline="") as stream:
                records = list(csv.DictReader(stream))
        else:
            raise ValueError("Catalog import supports JSON and CSV")
        offers = [self._offer_from_import(item, provider=provider) for item in records]
        return self.bulk_upsert(offers)

    @staticmethod
    def _offer_from_import(item: Any, *, provider: str) -> Offer:
        if not isinstance(item, dict):
            raise ValueError("Each catalog item must be an object")
        raw_attributes = item.get("attributes", {})
        if isinstance(raw_attributes, str):
            raw_attributes = json.loads(raw_attributes)
        if not isinstance(raw_attributes, dict):
            raise ValueError("attributes must be a mapping")
        source_id = str(item.get("source_item_id") or item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        article = str(item.get("article") or "").strip()
        if not title:
            raise ValueError("title is required")
        stable_key = source_id or article or normalize_catalog_text(title)
        # An explicit catalog id is the stable provider identity.  Generate an
        # id only when neither the public offer id nor source item id exists.
        offer_id = str(item.get("offer_id") or item.get("id") or "").strip()
        if not offer_id:
            offer_id = hashlib.sha256(f"{provider}:{stable_key}".encode()).hexdigest()[:24]
        provenance = item.get("data_provenance")
        if not isinstance(provenance, dict):
            provenance = {"source": str(item.get("source") or provider)}
        availability = item.get("availability")
        if isinstance(availability, str):
            normalized_availability = availability.strip().casefold()
            if not normalized_availability:
                availability = None
            elif normalized_availability in {"1", "true", "yes", "да", "доступно", "в наличии"}:
                availability = True
            elif normalized_availability in {"0", "false", "no", "нет", "недоступно", "под заказ"}:
                availability = False
            else:
                raise ValueError("availability must be a boolean or a recognized boolean string")
        elif availability not in (None, True, False, 0, 1):
            raise ValueError("availability must be boolean or null")
        return Offer(
            offer_id=offer_id,
            provider=provider,
            source_item_id=source_id,
            title=title,
            article=article,
            manufacturer=str(item.get("manufacturer") or ""),
            brand=str(item.get("brand") or ""),
            price=item.get("price"),
            currency=str(item.get("currency") or ""),
            price_unit=str(item.get("price_unit") or "шт."),
            availability=availability,
            availability_text=str(item.get("availability_text") or ""),
            url=str(item.get("url") or ""),
            attributes=raw_attributes,
            data_provenance=provenance,
        )
