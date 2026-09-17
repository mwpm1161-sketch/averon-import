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

from .models import EtmCatalogRecord, EtmManufacturer, parse_catalog_snapshot

_MAX_CATALOG_ITEMS = 500_000
_JOB_STATES = {0, 1, 2, 3}


@dataclass(frozen=True)
class EtmCatalogSyncResult:
    changed: bool
    item_count: int
    revision: str
    synced_at: str
    malformed_count: int = 0


@dataclass(frozen=True)
class EtmJobStatus:
    uuid: str = ""
    state: int | None = None
    url: str = ""
    error: str = ""
    revision: str = ""
    item_count: int = 0


def _now_marker() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _tokens(value: Any) -> list[str]:
    return [part for part in normalize_catalog_text(str(value or "")).split() if part]


class EtmCatalogMirror:
    """Independent transactional SgGds mirror and deterministic retrieval index."""

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
                CREATE TABLE IF NOT EXISTS etm_mirror_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS etm_catalog_products (
                    source_item_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    brand TEXT NOT NULL,
                    article TEXT NOT NULL,
                    brand_code TEXT NOT NULL,
                    cli_code TEXT NOT NULL,
                    class_code TEXT NOT NULL,
                    product_class TEXT NOT NULL,
                    normalized_article TEXT NOT NULL,
                    normalized_brand TEXT NOT NULL,
                    normalized_search TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_etm_article
                    ON etm_catalog_products(normalized_article);
                CREATE INDEX IF NOT EXISTS idx_etm_search
                    ON etm_catalog_products(normalized_search);
                CREATE TABLE IF NOT EXISTS etm_manufacturers (
                    code TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    normalized_label TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_etm_manufacturer_label
                    ON etm_manufacturers(normalized_label);
                """
            )
            for key in ("revision", "last_synced_at", "job_uuid", "job_state", "job_url", "job_error"):
                connection.execute(
                    "INSERT OR IGNORE INTO etm_mirror_meta(key,value) VALUES (?,?)",
                    (key, ""),
                )

    def _meta(self, key: str) -> str:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM etm_mirror_meta WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else ""

    @staticmethod
    def _set_meta(connection: sqlite3.Connection, key: str, value: Any) -> None:
        connection.execute(
            "INSERT INTO etm_mirror_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value or "")),
        )

    @property
    def revision(self) -> str:
        return self._meta("revision") or "unknown"

    @property
    def last_synced_at(self) -> str:
        return self._meta("last_synced_at")

    @property
    def job_uuid(self) -> str:
        return self._meta("job_uuid")

    @property
    def job_state(self) -> int | None:
        try:
            value = int(self._meta("job_state"))
            return value if value in _JOB_STATES else None
        except (TypeError, ValueError):
            return None

    @property
    def job_url(self) -> str:
        return self._meta("job_url")

    @property
    def job_error(self) -> str:
        return self._meta("job_error")

    def count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM etm_catalog_products").fetchone()[0])

    def has_content(self) -> bool:
        return self.count() > 0

    def stats(self) -> dict[str, Any]:
        return {
            "item_count": self.count(),
            "catalog_version": self.revision,
            "last_synced_at": self.last_synced_at,
            "job_uuid": self.job_uuid,
            "job_state": self.job_state,
            "job_error": self.job_error,
            "manufacturer_count": self.manufacturer_count(),
        }

    def manufacturer_count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM etm_manufacturers").fetchone()[0])

    def sync_snapshot(
        self,
        payload: Any,
        *,
        metadata: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> EtmCatalogSyncResult:
        try:
            products = parse_catalog_snapshot(payload)
        except ValueError as exc:
            raise SourcingProviderError(
                "ЭТМ iPRO вернул некорректный файл каталога",
                code="INVALID_CATALOG",
                category="invalid_response",
            ) from exc
        if len(products) > _MAX_CATALOG_ITEMS:
            raise SourcingProviderError(
                "Файл каталога ЭТМ iPRO превышает безопасный лимит",
                code="CATALOG_TOO_LARGE",
                category="invalid_response",
            )
        revision = self._fingerprint(products, metadata or {})
        synced_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        changed = revision != self.revision
        with self._connect() as connection:
            if changed:
                connection.execute("DELETE FROM etm_catalog_products")
                for product in products:
                    connection.execute(
                        """INSERT INTO etm_catalog_products(
                           source_item_id,name,brand,article,brand_code,cli_code,
                           class_code,product_class,normalized_article,normalized_brand,
                           normalized_search
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        self._record(product),
                    )
            self._set_meta(connection, "revision", revision)
            self._set_meta(connection, "last_synced_at", synced_at)
            self._set_meta(connection, "job_error", "")
        return EtmCatalogSyncResult(changed, len(products), revision, synced_at)

    def sync_manufacturers(self, manufacturers: tuple[EtmManufacturer, ...] | list[EtmManufacturer]) -> int:
        rows = {item.code: item for item in manufacturers if item.code and item.label}
        with self._connect() as connection:
            connection.execute("DELETE FROM etm_manufacturers")
            for item in sorted(rows.values(), key=lambda value: value.code):
                connection.execute(
                    "INSERT INTO etm_manufacturers(code,label,normalized_label) VALUES(?,?,?)",
                    (item.code, item.label, normalize_catalog_text(item.label)),
                )
        return len(rows)

    def resolve_manufacturer(self, label: str) -> str | None:
        normalized = normalize_catalog_text(label)
        if not normalized:
            return None
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT code FROM etm_manufacturers WHERE normalized_label=? ORDER BY code",
                (normalized,),
            ).fetchall()
        codes = {str(row[0]) for row in rows}
        return next(iter(codes)) if len(codes) == 1 else None

    def manufacturer_matches(self, label: str) -> tuple[str, ...]:
        normalized = normalize_catalog_text(label)
        if not normalized:
            return ()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT code FROM etm_manufacturers WHERE normalized_label=? ORDER BY code",
                (normalized,),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def search(
        self,
        intent: ProductIntent,
        *,
        limit: int = 20,
        manufacturer_code: str | None = None,
    ) -> list[EtmCatalogRecord]:
        article = normalize_catalog_text(intent.article)
        brand = normalize_catalog_text(intent.brand or intent.manufacturer)
        terms: list[str] = []
        for value in [
            *intent.search_queries,
            intent.normalized_name,
            intent.model,
            intent.product_class,
            intent.article,
            intent.brand,
            intent.manufacturer,
        ]:
            terms.extend(_tokens(value))
        terms = list(dict.fromkeys(terms))[:32]
        excluded_fallback_terms = set(_tokens(intent.article)) | set(_tokens(intent.brand)) | set(_tokens(intent.manufacturer))
        fallback_terms = [term for term in terms if term not in excluded_fallback_terms]
        if not terms and not article:
            return []
        candidate_limit = min(max(1, int(limit)) * 10, 500)
        with self._connect() as connection:
            rows: list[sqlite3.Row] = []
            if article:
                query = "SELECT * FROM etm_catalog_products WHERE normalized_article=?"
                params: list[Any] = [article]
                if manufacturer_code:
                    query += " AND brand_code=?"
                    params.append(manufacturer_code)
                query += " ORDER BY source_item_id LIMIT ?"
                params.append(candidate_limit)
                rows.extend(connection.execute(query, params).fetchall())
            # An article is a strong identifier.  A failed article lookup may
            # use independent name/model evidence, but never brand-only terms.
            if fallback_terms and not rows:
                clauses = " OR ".join("normalized_search LIKE ?" for _ in fallback_terms)
                params = [f"%{term}%" for term in fallback_terms]
                query = f"SELECT * FROM etm_catalog_products WHERE ({clauses})"
                if manufacturer_code:
                    query += " AND brand_code=?"
                    params.append(manufacturer_code)
                query += " ORDER BY source_item_id LIMIT ?"
                params.append(candidate_limit)
                rows.extend(connection.execute(query, params).fetchall())
        unique: dict[str, EtmCatalogRecord] = {}
        for row in rows:
            unique[row["source_item_id"]] = EtmCatalogRecord(
                source_item_id=row["source_item_id"], name=row["name"], brand=row["brand"],
                article=row["article"], brand_code=row["brand_code"], cli_code=row["cli_code"],
                class_code=row["class_code"], product_class=row["product_class"],
            )
        def score(product: EtmCatalogRecord) -> tuple[int, str]:
            product_article = normalize_catalog_text(product.article)
            product_brand = normalize_catalog_text(product.brand)
            score_value = 0
            if article and product_article == article:
                score_value += 1000
            if article and brand and product_article == article and product_brand == brand:
                score_value += 500
            if intent.model and normalize_catalog_text(intent.model) in normalize_catalog_text(product.name):
                score_value += 100
            score_value += sum(1 for term in terms if term in normalize_catalog_text(product.name + " " + product.product_class))
            return (-score_value, product.source_item_id)
        return [unique[key] for key in sorted(unique, key=lambda value: score(unique[value]))[: max(1, int(limit))]]

    def create_job(self, client: Any) -> EtmJobStatus:
        try:
            value = str(client.create_catalog_job()).strip()
        except SourcingProviderError:
            raise
        except Exception as exc:
            raise SourcingProviderError("Не удалось запустить синхронизацию каталога ЭТМ iPRO", category="upstream_error") from exc
        if not value:
            raise SourcingProviderError("ЭТМ iPRO не вернул идентификатор задания", category="invalid_response")
        with self._connect() as connection:
            self._set_meta(connection, "job_uuid", value)
            self._set_meta(connection, "job_state", 0)
            self._set_meta(connection, "job_url", "")
            self._set_meta(connection, "job_error", "")
        return EtmJobStatus(uuid=value, state=0, revision=self.revision, item_count=self.count())

    def update_job(self, client: Any) -> EtmJobStatus:
        value = self.job_uuid
        if not value:
            return EtmJobStatus(revision=self.revision, item_count=self.count())
        try:
            payload = client.get_catalog_job(value)
            data = payload.get("data", payload) if isinstance(payload, dict) else {}
            if not isinstance(data, dict):
                raise ValueError("job data must be an object")
            rows = data.get("rows")
            if not isinstance(rows, list) or not rows:
                raise ValueError("job response contains no rows")
            matching = [
                row for row in rows
                if isinstance(row, dict) and str(row.get("uuid", "")).strip() == value
            ]
            if len(matching) == 1:
                row = matching[0]
            elif not matching and len(rows) == 1 and isinstance(rows[0], dict):
                row = rows[0]
            else:
                raise ValueError("job response contains ambiguous rows")
            state = int(row.get("state"))
            if state not in _JOB_STATES:
                raise ValueError("unknown job state")
            urls = row.get("urls")
            url = ""
            if isinstance(urls, list):
                for candidate in urls:
                    if not isinstance(candidate, dict):
                        continue
                    candidate_url = candidate.get("url")
                    if isinstance(candidate_url, str) and candidate_url.strip():
                        url = candidate_url.strip()
                        break
        except SourcingProviderError:
            raise
        except (TypeError, ValueError) as exc:
            raise SourcingProviderError("ЭТМ iPRO вернул некорректный статус синхронизации", code="INVALID_RESPONSE", category="invalid_response") from exc
        with self._connect() as connection:
            self._set_meta(connection, "job_state", state)
            self._set_meta(connection, "job_url", url)
            self._set_meta(connection, "job_error", "" if state != 2 else "Синхронизация каталога ЭТМ iPRO завершилась с ошибкой")
        if state == 1:
            if not url:
                raise SourcingProviderError("ЭТМ iPRO не вернул файл каталога", code="INVALID_CATALOG", category="invalid_response")
            result = self.sync_snapshot(client.download_snapshot(url), metadata={"job_uuid": value, "url": url})
            with self._connect() as connection:
                self._set_meta(connection, "job_error", "")
            return EtmJobStatus(value, state, url, "", result.revision, result.item_count)
        return EtmJobStatus(
            value,
            state,
            url,
            "Каталог ЭТМ iPRO недоступен для синхронизации" if state == 2 else "",
            self.revision,
            self.count(),
        )

    @staticmethod
    def _record(product: EtmCatalogRecord) -> tuple[str, ...]:
        searchable = " ".join(
            [product.source_item_id, product.name, product.brand, product.article,
             product.brand_code, product.cli_code, product.class_code, product.product_class]
        )
        return (
            product.source_item_id, product.name, product.brand, product.article,
            product.brand_code, product.cli_code, product.class_code, product.product_class,
            normalize_catalog_text(product.article), normalize_catalog_text(product.brand),
            normalize_catalog_text(searchable),
        )

    @staticmethod
    def _fingerprint(products: tuple[EtmCatalogRecord, ...], metadata: dict[str, Any]) -> str:
        payload = {
            "metadata": metadata,
            "products": [item.model_dump(mode="json") for item in products],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
