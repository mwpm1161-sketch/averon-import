from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from averon_import.services.sourcing.models import ProductIntent
from averon_import.services.sourcing.providers.base import SourcingProviderError

from .models import TstnItemCategory, TstnItemDetail, TstnItemListResponse


TSTN_CATALOG_SCHEMA_REVISION = "tstn-local-catalog-v1"
TSTN_SEARCH_POLICY_REVISION = "unicode-token-index-v1"
TSTN_MAX_PAGE_SIZE = 100
# TN1 bounds one client to 64 attempts. Keep this mirror's complete sync within
# that bound, including the terminal page that proves a full last page ended.
TSTN_MAX_SYNC_PAGES = 64
TSTN_MAX_SYNC_ITEMS = 6_300
TSTN_MAX_RECORD_BYTES = 64 * 1024
TSTN_MAX_SNAPSHOT_BYTES = 128 * 1024 * 1024
TSTN_MAX_INDEX_TERMS_PER_ITEM = 512
TSTN_MAX_TOTAL_INDEX_TERMS = 5_000_000
TSTN_MAX_SEARCH_TERMS = 64
TSTN_MAX_SEARCH_RESULTS = 100
TSTN_MAX_SEARCH_CANDIDATES = 2_000

_TEXT_FIELDS = (
    "item_id",
    "article",
    "name",
    "brand",
    "subbrand",
    "barcode",
    "base_unit",
    "country_supplier",
)
_FIELD_WEIGHT = {
    "item_id": 1,
    "article": 1,
    "barcode": 1,
    "name": 5,
    "brand": 3,
    "subbrand": 3,
    "category": 2,
    "base_unit": 1,
    "country_supplier": 1,
}
_SEARCH_INDEX_REVISION_PREFIX = f"{TSTN_CATALOG_SCHEMA_REVISION}:{TSTN_SEARCH_POLICY_REVISION}:"
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class TstnCatalogRecord(BaseModel):
    """A local, non-commercial projection of documented TSTN ItemDetail fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    identity_kind: Literal["id", "article"]
    identity_value: str = Field(min_length=1, max_length=1024)
    id: str | None = Field(default=None, max_length=1024)
    article: str | None = None
    name: str | None = None
    brand: str | None = None
    subbrand: str | None = None
    categories: tuple[TstnItemCategory, ...] | None = None
    barcode: str | None = None
    base_unit: str | None = None
    units_list: tuple[Any, ...] | None = None
    date_create: str | None = None
    date_modify: str | None = None
    country_supplier: str | None = None


@dataclass(frozen=True, slots=True)
class TstnMirrorSyncResult:
    changed: bool
    item_count: int
    duplicate_count: int
    page_count: int
    revision: str
    search_index_revision: str
    synced_at: str


class TstnCatalogPageClient(Protocol):
    """The page method implemented by TN1's typed ``TstnApiClient``."""

    def list_items(
        self,
        *,
        access_token: str | SecretStr,
        category: int | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> TstnItemListResponse:
        ...


def _safe_error(message: str, *, code: str, category: str) -> SourcingProviderError:
    return SourcingProviderError(message, code=code, category=category)


def normalize_tstn_search_text(value: object) -> str:
    """Normalize text for retrieval only; source values remain unchanged."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    return " ".join("".join(char if char.isalnum() else " " for char in text).split())


def _normalize_code(value: object) -> str:
    """Ignore formatting punctuation while retaining every digit, including zeroes."""

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    return "".join(char for char in normalized if char.isalnum())


def _tokens(value: object) -> tuple[str, ...]:
    return tuple(token for token in normalize_tstn_search_text(value).split() if token)


def _decimal_json(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("non-finite decimal")
    parts = value.as_tuple()
    if len(parts.digits) > 1_000 or abs(parts.exponent) > 1_000:
        raise ValueError("decimal outside JSON bounds")
    rendered = str(value)
    if not re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?", rendered):
        raise ValueError("invalid decimal JSON value")
    return rendered


def _dump_json(value: Any) -> str:
    """Deterministic JSON that keeps Decimal values numeric and round-trippable."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, Decimal):
        return _decimal_json(value)
    if type(value) is int:
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("non-finite float")
        return json.dumps(value, allow_nan=False, separators=(",", ":"))
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, (tuple, list)):
        return "[" + ",".join(_dump_json(item) for item in value) + "]"
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return "{" + ",".join(
            _dump_json(key) + ":" + _dump_json(value[key])
            for key in sorted(value)
        ) + "}"
    raise ValueError("unsupported JSON value")


def _load_json(value: str) -> Any:
    def reject_constant(_constant: str) -> None:
        raise ValueError("non-standard JSON constant")

    return json.loads(value, parse_float=Decimal, parse_constant=reject_constant)


def _utc_marker(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _index_revision(catalog_revision: str) -> str:
    return hashlib.sha256(
        f"{_SEARCH_INDEX_REVISION_PREFIX}{catalog_revision}".encode("utf-8")
    ).hexdigest()


def _record_from_item(item: TstnItemDetail) -> TstnCatalogRecord:
    if not isinstance(item, TstnItemDetail):
        raise ValueError("typed ItemDetail expected")
    item_id = None if item.id is None else str(item.id)
    article = item.article
    if item_id:
        identity_kind: Literal["id", "article"] = "id"
        identity_value = item_id
    elif article:
        # The discriminator prevents an EKN/article value from colliding with
        # a numeric product ID that happens to have the same text.
        identity_kind = "article"
        identity_value = article
    else:
        raise ValueError("ItemDetail has no stable documented identifier")
    record = TstnCatalogRecord(
        identity_kind=identity_kind,
        identity_value=identity_value,
        id=item_id,
        article=item.article,
        name=item.name,
        brand=item.brand,
        subbrand=item.subbrand,
        categories=item.categories,
        barcode=item.barcode,
        base_unit=item.base_unit,
        units_list=item.units_list,
        date_create=item.date_create,
        date_modify=item.date_modify,
        country_supplier=item.country_supplier,
    )
    payload = _dump_json(record.model_dump(mode="python"))
    if len(payload.encode("utf-8")) > TSTN_MAX_RECORD_BYTES:
        raise ValueError("ItemDetail exceeds the record size limit")
    if _CONTROL_RE.search(payload):
        raise ValueError("ItemDetail contains a control character")
    return record


def _canonical_record(record: TstnCatalogRecord) -> str:
    return _dump_json(record.model_dump(mode="python"))


def _search_fields(record: TstnCatalogRecord) -> dict[str, tuple[str, ...]]:
    fields: dict[str, tuple[str, ...]] = {
        "item_id": _tokens(record.id),
        "article": _tokens(record.article),
        "name": _tokens(record.name),
        "brand": _tokens(record.brand),
        "subbrand": _tokens(record.subbrand),
        "barcode": _tokens(record.barcode),
        "base_unit": _tokens(record.base_unit),
        "country_supplier": _tokens(record.country_supplier),
    }
    category_terms: set[str] = set()
    for category in record.categories or ():
        category_terms.update(_tokens(category.name))
        if category.id is not None:
            category_terms.update(_tokens(str(category.id)))
        if category.parent is not None:
            category_terms.update(_tokens(str(category.parent)))
    fields["category"] = tuple(sorted(category_terms))
    unique_count = sum(len(set(terms)) for terms in fields.values())
    if unique_count > TSTN_MAX_INDEX_TERMS_PER_ITEM:
        raise ValueError("ItemDetail exceeds the index-term limit")
    return fields


def _snapshot_revision(records: Iterable[TstnCatalogRecord]) -> str:
    digest = hashlib.sha256()
    digest.update(TSTN_CATALOG_SCHEMA_REVISION.encode("ascii"))
    for record in sorted(records, key=lambda row: (row.identity_kind, row.identity_value)):
        line = _canonical_record(record).encode("utf-8")
        digest.update(len(line).to_bytes(8, "big"))
        digest.update(line)
    return digest.hexdigest()


class TstnCatalogMirror:
    """Offline SQLite mirror; only ``sync`` accepts a client, reads never do."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A persisted index is not trusted after process restart until one
        # complete integrity pass has validated its source rows and terms.
        # The validated revisions are only an in-process fast-path afterwards.
        self._integrity_validated: bool | None = None
        self._validated_catalog_revision: str | None = None
        self._validated_index_revision: str | None = None
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tstn_mirror_meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tstn_catalog_products(
                    identity_kind TEXT NOT NULL CHECK(identity_kind IN ('id','article')),
                    identity_value TEXT NOT NULL,
                    item_id TEXT,
                    article TEXT,
                    name TEXT,
                    brand TEXT,
                    subbrand TEXT,
                    categories_json TEXT NOT NULL,
                    barcode TEXT,
                    base_unit TEXT,
                    units_list_json TEXT NOT NULL,
                    date_create TEXT,
                    date_modify TEXT,
                    country_supplier TEXT,
                    normalized_article TEXT NOT NULL,
                    canonical_payload TEXT NOT NULL,
                    PRIMARY KEY(identity_kind, identity_value)
                );
                CREATE INDEX IF NOT EXISTS idx_tstn_product_id
                    ON tstn_catalog_products(item_id);
                CREATE INDEX IF NOT EXISTS idx_tstn_article
                    ON tstn_catalog_products(article);
                CREATE INDEX IF NOT EXISTS idx_tstn_article_normalized
                    ON tstn_catalog_products(normalized_article);
                CREATE TABLE IF NOT EXISTS tstn_search_terms(
                    identity_kind TEXT NOT NULL,
                    identity_value TEXT NOT NULL,
                    field TEXT NOT NULL,
                    term TEXT NOT NULL,
                    PRIMARY KEY(identity_kind, identity_value, field, term),
                    FOREIGN KEY(identity_kind, identity_value)
                        REFERENCES tstn_catalog_products(identity_kind, identity_value)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_tstn_search_term
                    ON tstn_search_terms(term, field, identity_kind, identity_value);
                """
            )
            for key, value in (
                ("schema_revision", TSTN_CATALOG_SCHEMA_REVISION),
                ("catalog_revision", ""),
                ("search_index_revision", ""),
                ("item_count", "0"),
                ("last_synced_at", ""),
            ):
                connection.execute(
                    "INSERT OR IGNORE INTO tstn_mirror_meta(key,value) VALUES(?,?)",
                    (key, value),
                )
            schema_revision = self._meta_from(connection, "schema_revision")
            if schema_revision != TSTN_CATALOG_SCHEMA_REVISION:
                raise _safe_error(
                    "Локальный каталог ТСТН имеет несовместимую схему",
                    code="TSTN_CATALOG_SCHEMA_MISMATCH",
                    category="storage_error",
                )

    @staticmethod
    def _meta_from(connection: sqlite3.Connection, key: str) -> str:
        row = connection.execute(
            "SELECT value FROM tstn_mirror_meta WHERE key=?", (key,)
        ).fetchone()
        return str(row[0]) if row else ""

    @staticmethod
    def _set_meta(connection: sqlite3.Connection, key: str, value: object) -> None:
        connection.execute(
            "INSERT INTO tstn_mirror_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def _meta(self, key: str) -> str:
        with self._connect() as connection:
            return self._meta_from(connection, key)

    @property
    def revision(self) -> str:
        return self._meta("catalog_revision") or "unknown"

    @property
    def search_index_revision(self) -> str:
        return self._meta("search_index_revision")

    @property
    def last_synced_at(self) -> str:
        return self._meta("last_synced_at")

    @property
    def search_index_ready(self) -> bool:
        try:
            with self._connect() as connection:
                revision = self._meta_from(connection, "catalog_revision")
                index_revision = self._meta_from(connection, "search_index_revision")
            return (
                self._integrity_validated is True
                and revision != ""
                and revision == self._validated_catalog_revision
                and index_revision == self._validated_index_revision
                and index_revision == _index_revision(revision)
            )
        except Exception:
            return False

    def count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM tstn_catalog_products").fetchone()[0])

    def has_content(self) -> bool:
        return self.count() > 0

    def check_integrity(self) -> bool:
        """Check SQLite, identity rows, foreign keys, and index revision locally."""
        revision = ""
        index_revision = ""
        try:
            with self._connect() as connection:
                revision = self._meta_from(connection, "catalog_revision")
                index_revision = self._meta_from(connection, "search_index_revision")
                valid = self._check_integrity_connection(connection)
        except Exception:
            valid = False
        self._remember_integrity_result(valid, revision, index_revision)
        return valid

    @staticmethod
    def _check_integrity_connection(connection: sqlite3.Connection) -> bool:
        """Perform the full diagnostic using an existing SQLite transaction."""

        try:
            quick = connection.execute("PRAGMA quick_check").fetchone()
            if quick is None or str(quick[0]).casefold() != "ok":
                return False
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                return False
            rows = connection.execute(
                "SELECT * FROM tstn_catalog_products ORDER BY identity_kind,identity_value"
            ).fetchall()
            product_count = len(rows)
            meta_count = int(TstnCatalogMirror._meta_from(connection, "item_count") or "0")
            if product_count != meta_count:
                return False
            revision = TstnCatalogMirror._meta_from(connection, "catalog_revision")
            index_revision = TstnCatalogMirror._meta_from(connection, "search_index_revision")
            if revision:
                if index_revision != _index_revision(revision):
                    return False
            elif product_count or index_revision:
                return False
            records: list[TstnCatalogRecord] = []
            for row in rows:
                record = TstnCatalogRecord.model_validate(_load_json(str(row["canonical_payload"])))
                identity = (str(row["identity_kind"]), str(row["identity_value"]))
                if identity != (record.identity_kind, record.identity_value):
                    return False
                expected_columns = (
                    record.id,
                    record.article,
                    record.name,
                    record.brand,
                    record.subbrand,
                    _dump_json([item.model_dump(mode="python") for item in record.categories or ()]),
                    record.barcode,
                    record.base_unit,
                    _dump_json(record.units_list or []),
                    record.date_create,
                    record.date_modify,
                    record.country_supplier,
                    _normalize_code(record.article),
                    _canonical_record(record),
                )
                actual_columns = tuple(row[key] for key in (
                    "item_id",
                    "article",
                    "name",
                    "brand",
                    "subbrand",
                    "categories_json",
                    "barcode",
                    "base_unit",
                    "units_list_json",
                    "date_create",
                    "date_modify",
                    "country_supplier",
                    "normalized_article",
                    "canonical_payload",
                ))
                if actual_columns != expected_columns:
                    return False
                expected_terms = {
                    (field, term)
                    for field, terms in _search_fields(record).items()
                    for term in terms
                }
                actual_terms = {
                    (str(term_row[0]), str(term_row[1]))
                    for term_row in connection.execute(
                        "SELECT field,term FROM tstn_search_terms "
                        "WHERE identity_kind=? AND identity_value=?",
                        identity,
                    ).fetchall()
                }
                if expected_terms != actual_terms:
                    return False
                records.append(record)
            if revision and _snapshot_revision(records) != revision:
                return False
            return True
        except Exception:
            return False

    def _remember_integrity_result(
        self, valid: bool, revision: str, index_revision: str
    ) -> None:
        self._integrity_validated = valid
        self._validated_catalog_revision = revision
        self._validated_index_revision = index_revision

    def _invalidate_integrity_cache(self) -> None:
        self._integrity_validated = None
        self._validated_catalog_revision = None
        self._validated_index_revision = None

    def _ensure_search_integrity(self) -> bool:
        """Validate once per persisted revision, rescanning only after invalidation."""

        try:
            with self._connect() as connection:
                revision = self._meta_from(connection, "catalog_revision")
                index_revision = self._meta_from(connection, "search_index_revision")
        except Exception:
            self._remember_integrity_result(False, "", "")
            return False
        if (
            self._integrity_validated is not None
            and revision == self._validated_catalog_revision
            and index_revision == self._validated_index_revision
        ):
            return self._integrity_validated
        return self.check_integrity()

    def stats(self) -> dict[str, Any]:
        try:
            with self._connect() as connection:
                item_count = int(self._meta_from(connection, "item_count") or "0")
                revision = self._meta_from(connection, "catalog_revision") or "unknown"
                index_revision = self._meta_from(connection, "search_index_revision")
                last_synced_at = self._meta_from(connection, "last_synced_at")
        except Exception:
            item_count, revision, index_revision, last_synced_at = 0, "unknown", "", ""
        current_validation = (
            self._integrity_validated
            if revision == self._validated_catalog_revision
            and index_revision == self._validated_index_revision
            else None
        )
        return {
            "item_count": item_count,
            "catalog_revision": revision,
            "search_index_revision": index_revision,
            "search_index_ready": self.search_index_ready,
            "integrity_ok": current_validation,
            "last_synced_at": last_synced_at,
        }

    def sync(
        self,
        client: TstnCatalogPageClient,
        *,
        access_token: str | SecretStr,
        page_size: int = TSTN_MAX_PAGE_SIZE,
        max_pages: int = TSTN_MAX_SYNC_PAGES,
        max_items: int | None = None,
        max_snapshot_bytes: int = TSTN_MAX_SNAPSHOT_BYTES,
        now: datetime | None = None,
    ) -> TstnMirrorSyncResult:
        """Fetch a bounded snapshot through an injected typed client, then swap atomically."""

        if (isinstance(page_size, bool) or not isinstance(page_size, int)
                or not 1 <= page_size <= TSTN_MAX_PAGE_SIZE
                or isinstance(max_pages, bool) or not isinstance(max_pages, int)
                or not 2 <= max_pages <= TSTN_MAX_SYNC_PAGES):
            raise _safe_error(
                "Для синхронизации каталога ТСТН заданы неверные ограничения",
                code="TSTN_SYNC_LIMIT_INVALID",
                category="invalid_request",
            )
        effective_item_limit = min(TSTN_MAX_SYNC_ITEMS, (max_pages - 1) * page_size)
        if max_items is None:
            item_limit = effective_item_limit
        elif (isinstance(max_items, bool) or not isinstance(max_items, int)
              or not 1 <= max_items <= effective_item_limit):
            raise _safe_error(
                "Для синхронизации каталога ТСТН задан неверный лимит товаров",
                code="TSTN_SYNC_LIMIT_INVALID",
                category="invalid_request",
            )
        else:
            item_limit = max_items
        if (isinstance(max_snapshot_bytes, bool) or not isinstance(max_snapshot_bytes, int)
                or not 1 <= max_snapshot_bytes <= TSTN_MAX_SNAPSHOT_BYTES):
            raise _safe_error(
                "Для синхронизации каталога ТСТН задан неверный лимит снимка",
                code="TSTN_SYNC_LIMIT_INVALID",
                category="invalid_request",
            )
        if client is None or not callable(getattr(client, "list_items", None)):
            raise _safe_error(
                "Клиент каталога ТСТН не настроен",
                code="TSTN_CLIENT_MISSING",
                category="not_configured",
            )

        records: dict[tuple[str, str], tuple[TstnCatalogRecord, str, dict[str, tuple[str, ...]]]] = {}
        raw_count = 0
        duplicate_count = 0
        snapshot_bytes = 0
        total_index_terms = 0
        page_signatures: set[str] = set()
        observed_totals: set[int] = set()
        page_count = 0
        terminal = False
        last_count_all: int | None = None
        try:
            for page_index in range(max_pages):
                offset = page_index * page_size
                page = client.list_items(
                    access_token=access_token,
                    limit=page_size,
                    offset=offset,
                )
                page_count += 1
                if not isinstance(page, TstnItemListResponse) or page.results is None:
                    raise ValueError("typed catalog page with results is required")
                if len(page.results) > page_size:
                    raise ValueError("catalog page exceeds requested size")
                if page.page_num is not None and page.page_num != page_index + 1:
                    raise ValueError("catalog page number is inconsistent")
                if page.count is not None and page.count < len(page.results):
                    raise ValueError("catalog page count is inconsistent")
                if page.count_all is None:
                    raise ValueError("catalog total is required to prove snapshot completeness")
                observed_totals.add(page.count_all)
                last_count_all = page.count_all
                if page.count_all < offset + len(page.results):
                    raise ValueError("catalog total is below the observed offset")
                if len(observed_totals) > 1:
                    raise ValueError("catalog total changed during pagination")

                page_records: list[tuple[TstnCatalogRecord, str, dict[str, tuple[str, ...]]]] = []
                for item in page.results:
                    record = _record_from_item(item)
                    canonical = _canonical_record(record)
                    encoded_size = len(canonical.encode("utf-8"))
                    snapshot_bytes += encoded_size
                    if snapshot_bytes > max_snapshot_bytes:
                        raise OverflowError("snapshot byte limit exceeded")
                    terms_by_field = _search_fields(record)
                    term_count = sum(len(terms) for terms in terms_by_field.values())
                    total_index_terms += term_count
                    if total_index_terms > TSTN_MAX_TOTAL_INDEX_TERMS:
                        raise OverflowError("index term limit exceeded")
                    page_records.append((record, canonical, terms_by_field))
                page_digest = hashlib.sha256(
                    _dump_json(sorted(
                        ((record.identity_kind, record.identity_value, canonical)
                         for record, canonical, _terms in page_records)
                    )).encode("utf-8")
                ).hexdigest()
                if page_digest in page_signatures and page_records:
                    raise ValueError("repeated catalog page")
                page_signatures.add(page_digest)

                for record, canonical, terms_by_field in page_records:
                    identity = (record.identity_kind, record.identity_value)
                    existing = records.get(identity)
                    if existing is not None:
                        if existing[1] != canonical:
                            raise ValueError("conflicting records share a catalog identity")
                        duplicate_count += 1
                        continue
                    records[identity] = (record, canonical, terms_by_field)
                raw_count += len(page.results)
                if raw_count > item_limit:
                    raise OverflowError("catalog item limit exceeded")
                if len(page.results) < page_size:
                    terminal = True
                    break
            if not terminal:
                raise OverflowError("catalog page limit exceeded before terminal page")
            # A stable count_all confirms traversal coverage, but does not make
            # the remote pagination a transactional snapshot.
            if last_count_all is None or last_count_all != raw_count:
                raise ValueError("catalog total does not match the completed traversal")
            if raw_count == 0 and last_count_all != 0:
                raise ValueError("empty catalog page contradicts catalog total")
        except OverflowError:
            raise _safe_error(
                "Каталог ТСТН превышает безопасные ограничения синхронизации",
                code="TSTN_SNAPSHOT_LIMIT",
                category="resource_limit",
            ) from None
        except Exception:
            # The upstream client or fake may include a credential-bearing URL
            # in exception text. Keep only a fixed local diagnostic.
            raise _safe_error(
                "Снимок каталога ТСТН неполный или противоречивый",
                code="TSTN_SNAPSHOT_INVALID",
                category="invalid_response",
            ) from None

        sorted_records = [records[key][0] for key in sorted(records)]
        revision = _snapshot_revision(sorted_records)
        index_revision = _index_revision(revision)
        changed = revision != self.revision
        replace_snapshot = changed or not self.check_integrity()
        synced_at = _utc_marker(now)
        self._invalidate_integrity_cache()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if replace_snapshot:
                    connection.execute("DELETE FROM tstn_catalog_products")
                    self._insert_snapshot(connection, records)
                elif not self.search_index_ready:
                    connection.execute("DELETE FROM tstn_search_terms")
                    self._insert_search_terms(connection, records)
                self._set_meta(connection, "catalog_revision", revision)
                self._set_meta(connection, "search_index_revision", index_revision)
                self._set_meta(connection, "item_count", len(records))
                self._set_meta(connection, "last_synced_at", synced_at)
                if not self._check_integrity_connection(connection):
                    raise ValueError("new snapshot failed local integrity validation")
        except Exception:
            raise _safe_error(
                "Локальный каталог ТСТН не обновлён; предыдущая ревизия сохранена",
                code="TSTN_MIRROR_UPDATE_FAILED",
                category="storage_error",
            ) from None
        self._remember_integrity_result(True, revision, index_revision)
        return TstnMirrorSyncResult(
            changed=changed,
            item_count=len(records),
            duplicate_count=duplicate_count,
            page_count=page_count,
            revision=revision,
            search_index_revision=index_revision,
            synced_at=synced_at,
        )

    def _insert_snapshot(
        self,
        connection: sqlite3.Connection,
        records: dict[tuple[str, str], tuple[TstnCatalogRecord, str, dict[str, tuple[str, ...]]]],
    ) -> None:
        for key in sorted(records):
            record, canonical, terms_by_field = records[key]
            data = record.model_dump(mode="python")
            connection.execute(
                """INSERT INTO tstn_catalog_products(
                    identity_kind,identity_value,item_id,article,name,brand,subbrand,
                    categories_json,barcode,base_unit,units_list_json,date_create,
                    date_modify,country_supplier,normalized_article,canonical_payload
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record.identity_kind,
                    record.identity_value,
                    record.id,
                    record.article,
                    record.name,
                    record.brand,
                    record.subbrand,
                    _dump_json(data["categories"] or []),
                    record.barcode,
                    record.base_unit,
                    _dump_json(data["units_list"] or []),
                    record.date_create,
                    record.date_modify,
                    record.country_supplier,
                    _normalize_code(record.article),
                    canonical,
                ),
            )
            self._insert_terms_for_record(connection, record, terms_by_field)

    def _insert_search_terms(
        self,
        connection: sqlite3.Connection,
        records: dict[tuple[str, str], tuple[TstnCatalogRecord, str, dict[str, tuple[str, ...]]]],
    ) -> None:
        for key in sorted(records):
            record, _canonical, terms_by_field = records[key]
            self._insert_terms_for_record(connection, record, terms_by_field)

    @staticmethod
    def _insert_terms_for_record(
        connection: sqlite3.Connection,
        record: TstnCatalogRecord,
        terms_by_field: dict[str, tuple[str, ...]],
    ) -> None:
        rows = [
            (record.identity_kind, record.identity_value, field, term)
            for field, terms in terms_by_field.items()
            for term in sorted(set(terms))
        ]
        connection.executemany(
            "INSERT INTO tstn_search_terms(identity_kind,identity_value,field,term) "
            "VALUES(?,?,?,?)",
            rows,
        )

    def rebuild_search_index(self) -> str:
        """Recreate the local token index from stored documented source fields."""

        revision = self.revision
        if revision == "unknown":
            raise _safe_error(
                "Локальный каталог ТСТН ещё не загружен",
                code="TSTN_CATALOG_NOT_READY",
                category="not_configured",
            )
        self._invalidate_integrity_cache()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                revision = self._meta_from(connection, "catalog_revision")
                if not revision:
                    raise ValueError("catalog revision is missing")
                connection.execute("DELETE FROM tstn_search_terms")
                rows = connection.execute(
                    "SELECT canonical_payload FROM tstn_catalog_products "
                    "ORDER BY identity_kind,identity_value"
                ).fetchall()
                index_terms = 0
                for row in rows:
                    record = TstnCatalogRecord.model_validate(_load_json(str(row[0])))
                    fields = _search_fields(record)
                    index_terms += sum(len(value) for value in fields.values())
                    if index_terms > TSTN_MAX_TOTAL_INDEX_TERMS:
                        raise OverflowError("index term limit exceeded")
                    self._insert_terms_for_record(connection, record, fields)
                index_revision = _index_revision(revision)
                self._set_meta(connection, "search_index_revision", index_revision)
                if not self._check_integrity_connection(connection):
                    raise ValueError("rebuilt index failed local integrity validation")
        except Exception:
            raise _safe_error(
                "Локальный поисковый индекс ТСТН не перестроен",
                code="TSTN_INDEX_REBUILD_FAILED",
                category="storage_error",
            ) from None
        self._remember_integrity_result(True, revision, index_revision)
        return index_revision

    def search(
        self,
        intent: ProductIntent | str | None = None,
        *,
        item_id: str | int | None = None,
        article: str | None = None,
        name: str | None = None,
        brand: str | None = None,
        subbrand: str | None = None,
        category: str | int | None = None,
        limit: int = 20,
    ) -> list[TstnCatalogRecord]:
        """Return deterministic local catalog candidates, never an Offer or MATCH."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= TSTN_MAX_SEARCH_RESULTS:
            raise _safe_error(
                "Задан неверный лимит локального поиска ТСТН",
                code="TSTN_SEARCH_LIMIT_INVALID",
                category="invalid_request",
            )
        if intent is not None and not isinstance(intent, (ProductIntent, str)):
            raise _safe_error(
                "Задан неверный запрос локального поиска ТСТН",
                code="TSTN_SEARCH_QUERY_INVALID",
                category="invalid_request",
            )
        if isinstance(intent, str):
            name = " ".join(part for part in (intent, name or "") if part)
        elif isinstance(intent, ProductIntent):
            article = article or intent.article
            name_parts = [intent.normalized_name, intent.model, *intent.search_queries, name or ""]
            name = " ".join(part for part in name_parts if part)
            brand = " ".join(part for part in (intent.manufacturer, intent.brand, brand or "") if part)
        raw_query_values = (item_id, article, name, brand, subbrand, category)
        invalid_query_type = (
            item_id is not None and (isinstance(item_id, bool) or not isinstance(item_id, (str, int)))
            or any(value is not None and not isinstance(value, str) for value in (article, name, brand, subbrand))
            or category is not None and (isinstance(category, bool) or not isinstance(category, (str, int)))
        )
        if invalid_query_type or any(
            value is not None and len(str(value)) > 4_096 for value in raw_query_values
        ):
            raise _safe_error(
                "Запрос локального поиска ТСТН превышает безопасные ограничения",
                code="TSTN_SEARCH_QUERY_INVALID",
                category="invalid_request",
            )
        query_terms = tuple(dict.fromkeys(_tokens(" ".join(filter(None, (
            name,
            brand,
            subbrand,
            str(category) if category is not None else "",
        ))))))[:TSTN_MAX_SEARCH_TERMS]
        exact_id = None if item_id is None else str(item_id).strip()
        exact_article = None if article is None else article.strip()
        normalized_article = _normalize_code(exact_article)
        if not any((exact_id, exact_article, query_terms)):
            return []
        if not self._ensure_search_integrity():
            raise _safe_error(
                "Локальный каталог ТСТН или его индекс не прошёл проверку",
                code="TSTN_CATALOG_NOT_READY",
                category="storage_error",
            )

        scores: dict[tuple[str, str], int] = {}
        with self._connect() as connection:
            if exact_id:
                rows = connection.execute(
                    "SELECT identity_kind,identity_value FROM tstn_catalog_products WHERE item_id=?",
                    (exact_id,),
                ).fetchall()
                for row in rows:
                    scores[(str(row[0]), str(row[1]))] = 1_000_000
            if exact_article:
                rows = connection.execute(
                    "SELECT identity_kind,identity_value,article FROM tstn_catalog_products "
                    "WHERE article=? OR (?<>'' AND normalized_article=?)",
                    (exact_article, normalized_article, normalized_article),
                ).fetchall()
                for row in rows:
                    key = (str(row[0]), str(row[1]))
                    weight = 900_000 if str(row[2] or "") == exact_article else 800_000
                    scores[key] = max(scores.get(key, 0), weight)
            if query_terms:
                placeholders = ",".join("?" for _ in query_terms)
                rows = connection.execute(
                    f"""SELECT identity_kind,identity_value,
                        SUM(CASE field
                            WHEN 'name' THEN { _FIELD_WEIGHT['name'] }
                            WHEN 'brand' THEN { _FIELD_WEIGHT['brand'] }
                            WHEN 'subbrand' THEN { _FIELD_WEIGHT['subbrand'] }
                            WHEN 'category' THEN { _FIELD_WEIGHT['category'] }
                            ELSE 1 END) AS relevance,
                        COUNT(DISTINCT term) AS matched_terms
                        FROM tstn_search_terms
                        WHERE term IN ({placeholders})
                        GROUP BY identity_kind,identity_value
                        ORDER BY relevance DESC,matched_terms DESC,identity_kind,identity_value
                        LIMIT ?""",
                    [*query_terms, TSTN_MAX_SEARCH_CANDIDATES],
                ).fetchall()
                for row in rows:
                    key = (str(row[0]), str(row[1]))
                    score = int(row[2] or 0) * 100 + int(row[3] or 0)
                    scores[key] = max(scores.get(key, 0), score)
            if not scores:
                return []
            ordered = sorted(scores, key=lambda key: (-scores[key], key[0], key[1]))[:limit]
            candidates: list[TstnCatalogRecord] = []
            for identity_kind, identity_value in ordered:
                row = connection.execute(
                    "SELECT canonical_payload FROM tstn_catalog_products "
                    "WHERE identity_kind=? AND identity_value=?",
                    (identity_kind, identity_value),
                ).fetchone()
                if row is None:
                    raise _safe_error(
                        "Локальный индекс ТСТН ссылается на отсутствующий товар",
                        code="TSTN_INDEX_INTEGRITY_ERROR",
                        category="storage_error",
                    )
                try:
                    candidates.append(TstnCatalogRecord.model_validate(_load_json(str(row[0]))))
                except Exception:
                    raise _safe_error(
                        "Запись локального каталога ТСТН повреждена",
                        code="TSTN_RECORD_INVALID",
                        category="storage_error",
                    ) from None
        return candidates
