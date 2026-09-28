from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
from typing import Any
import unicodedata

from averon_import.core.unit_normalization import normalize_unit_family
from averon_import.services.one_c_history.models import ImportProfile, utc_now
from averon_import.services.one_c_history.read_model import (
    OneCHistoryCatalogSnapshot,
    OneCHistoryEvent,
    OneCHistoryItem,
    OneCHistoryReadError,
    OneCHistoryVariant,
)
from averon_import.services.one_c_history.xlsx_import import PARSER_VERSION, ParsedWorkbook


class OneCHistoryRepository:
    """Owns 1C history snapshot and import profiles, separate from the offer catalog."""

    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / "sourcing" / "one_c"
        self.root.mkdir(parents=True, exist_ok=True)
        self.database_path = self.root / "history.sqlite3"
        self.profiles_path = self.root / "import_profiles.json"
        self.status_path = self.root / "import_status.json"
        self._lock = threading.RLock()
        self.max_profiles = 100

    def _connect(self, path: Path | None = None) -> sqlite3.Connection:
        connection = sqlite3.connect(path or self.database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        try:
            if path.stat().st_size > 1_048_576:
                return default
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except (OSError, ValueError, TypeError):
            return default

    @staticmethod
    def _atomic_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def profiles(self) -> list[ImportProfile]:
        raw = self._read_json(self.profiles_path, {"profiles": []})
        profiles = []
        for item in raw.get("profiles", []) if isinstance(raw, dict) else []:
            try:
                profiles.append(ImportProfile.model_validate(item))
            except Exception:
                continue
        return sorted(profiles, key=lambda profile: (profile.name.casefold(), profile.profile_id))

    def profile(self, profile_id: str) -> ImportProfile | None:
        return next((item for item in self.profiles() if item.profile_id == profile_id), None)

    def compatible_profiles(self, *, sheet_name: str, signature: str) -> list[ImportProfile]:
        return [
            item for item in self.profiles()
            if item.sheet_name == sheet_name
            and item.header_signature == signature
            and item.parser_version == PARSER_VERSION
        ]

    def compatible_profile(self, *, sheet_name: str, signature: str) -> ImportProfile | None:
        """Return a profile only when compatibility identifies exactly one choice."""
        candidates = self.compatible_profiles(sheet_name=sheet_name, signature=signature)
        return candidates[0] if len(candidates) == 1 else None

    def profiles_for_sheet(self, sheet_name: str) -> list[ImportProfile]:
        return [item for item in self.profiles() if item.sheet_name == sheet_name]

    def save_profile(self, profile: ImportProfile) -> ImportProfile:
        with self._lock:
            existing = {item.profile_id: item for item in self.profiles()}
            previous = existing.get(profile.profile_id)
            if previous is None and len(existing) >= self.max_profiles:
                raise ValueError("Достигнут лимит сохранённых профилей импорта.")
            if previous:
                profile = profile.model_copy(update={
                    "created_at": previous.created_at,
                    "updated_at": utc_now(),
                    "revision": previous.revision + 1,
                })
            existing[profile.profile_id] = profile
            self._atomic_json(
                self.profiles_path,
                {"schema_version": 1, "profiles": [item.model_dump(mode="json") for item in existing.values()]},
            )
            return profile

    def delete_profile(self, profile_id: str) -> bool:
        with self._lock:
            existing = {item.profile_id: item for item in self.profiles()}
            if profile_id not in existing:
                return False
            del existing[profile_id]
            self._atomic_json(
                self.profiles_path,
                {"schema_version": 1, "profiles": [item.model_dump(mode="json") for item in existing.values()]},
            )
            return True

    def record_attempt(self, status: str, *, file_sha256: str | None = None, warning_count: int = 0, error_code: str | None = None) -> None:
        payload = self._read_json(self.status_path, {})
        payload["last_attempt"] = {
            "status": status,
            "attempted_at": utc_now(),
            "sha256": file_sha256,
            "warning_count": max(0, int(warning_count)),
            "error_code": error_code,
        }
        self._atomic_json(self.status_path, payload)

    def active_metadata(self) -> dict | None:
        if not self.database_path.is_file():
            return None
        with self._lock:
            connection: sqlite3.Connection | None = None
            try:
                connection = self._connect()
                rows = connection.execute("SELECT key, value FROM import_metadata").fetchall()
                metadata = {str(row["key"]): json.loads(row["value"]) for row in rows}
                return metadata or None
            except (OSError, sqlite3.Error, ValueError, TypeError):
                return None
            finally:
                if connection is not None:
                    connection.close()

    @staticmethod
    def _metadata_and_version(connection: sqlite3.Connection) -> tuple[dict[str, Any], str]:
        try:
            rows = connection.execute(
                "SELECT key, value FROM import_metadata "
                "WHERE key IN ('sha256','semantic_import_fingerprint','item_count','event_count')"
            ).fetchall()
            values = {str(row["key"]): json.loads(row["value"]) for row in rows}
            source_sha = values.get("sha256")
            semantic_fingerprint = values.get("semantic_import_fingerprint")
            if not (
                isinstance(source_sha, str)
                and len(source_sha) == 64
                and all(char in "0123456789abcdef" for char in source_sha.casefold())
                and isinstance(semantic_fingerprint, str)
                and len(semantic_fingerprint) == 64
                and all(char in "0123456789abcdef" for char in semantic_fingerprint.casefold())
            ):
                raise ValueError("missing snapshot identity")
            if any(type(values.get(key)) is not int or values[key] < 0 for key in ("item_count", "event_count")):
                raise ValueError("invalid snapshot counts")
            identity = f"{source_sha.casefold()}:{semantic_fingerprint.casefold()}".encode("ascii")
            version = "1c-" + hashlib.sha256(identity).hexdigest()[:24]
            return values, version
        except (sqlite3.Error, ValueError, TypeError, UnicodeError) as exc:
            raise OneCHistoryReadError("История закупок недоступна") from exc

    def catalog_version(self) -> str | None:
        """Return a deterministic version for the active snapshot, or None if absent."""

        if not self.database_path.is_file():
            return None
        with self._lock:
            connection: sqlite3.Connection | None = None
            try:
                connection = self._connect()
                _, version = self._metadata_and_version(connection)
                return version
            except OneCHistoryReadError:
                raise
            except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
                raise OneCHistoryReadError("История закупок недоступна") from exc
            finally:
                if connection is not None:
                    connection.close()

    @staticmethod
    def _json_object(value: Any) -> tuple[dict[str, Any], bool]:
        try:
            parsed = json.loads(value) if isinstance(value, str) else None
        except (ValueError, TypeError):
            return {}, False
        return (parsed, True) if isinstance(parsed, dict) else ({}, False)

    @staticmethod
    def _decimal(value: Any) -> tuple[Decimal | None, bool]:
        if value is None:
            return None, True
        if not isinstance(value, str):
            return None, False
        if value == "":
            return None, True
        try:
            parsed = Decimal(str(value))
            return (parsed, parsed.is_finite()) if parsed.is_finite() else (None, False)
        except (InvalidOperation, TypeError, ValueError):
            return None, False

    @staticmethod
    def _source_text(value: Any) -> tuple[str, bool]:
        if value is None:
            return "", True
        if isinstance(value, str):
            return value, True
        return "", False

    @staticmethod
    def _source_row(value: Any) -> tuple[int, bool]:
        try:
            return int(value), True
        except (TypeError, ValueError, OverflowError):
            return 0, False

    @staticmethod
    def _identity_fact_key(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
        return re.sub(r"[^a-zа-я0-9]+", "", normalized)

    @staticmethod
    def _item_code_key(value: str) -> str:
        return unicodedata.normalize("NFKC", value).strip()

    def read_catalog_snapshot(self) -> OneCHistoryCatalogSnapshot | None:
        """Read one consistent immutable projection while activation is excluded."""

        if not self.database_path.is_file():
            return None
        with self._lock:
            connection: sqlite3.Connection | None = None
            try:
                connection = self._connect()
                metadata, version = self._metadata_and_version(connection)
                item_rows = connection.execute(
                    "SELECT item_id,source_item_code,display_name,raw_unit,unit_family,"
                    "identity_quality,group_number,descriptive_facts_json "
                    "FROM nomenclature_items ORDER BY item_id"
                ).fetchall()
                variant_rows = connection.execute(
                    "SELECT variant_id,item_id,item_name,raw_unit,unit_family,article,"
                    "manufacturer,characteristic,first_source_row "
                    "FROM nomenclature_descriptive_variants "
                    "ORDER BY item_id,item_name,variant_id"
                ).fetchall()
                event_rows = connection.execute(
                    "SELECT event_id,item_id,item_code,item_name,document_date,document_type,counterparty,"
                    "quantity_decimal,reported_unit_price_gross_decimal,"
                    "effective_unit_price_gross_decimal,amount_gross_decimal,price_usable,"
                    "price_basis,raw_unit,unit_family,source_row,optional_facts_json "
                    "FROM purchase_events ORDER BY item_id,document_date DESC,source_row DESC,event_id DESC"
                ).fetchall()

                item_payload: dict[str, dict[str, Any]] = {}
                for row in item_rows:
                    item_id, id_valid = self._source_text(row["item_id"])
                    display_name, name_valid = self._source_text(row["display_name"])
                    raw_unit, unit_valid = self._source_text(row["raw_unit"])
                    identity_quality, quality_valid = self._source_text(row["identity_quality"])
                    source_code, code_valid = self._source_text(row["source_item_code"])
                    family, family_valid = self._source_text(row["unit_family"])
                    facts, facts_valid = self._json_object(row["descriptive_facts_json"])
                    group_number, group_valid = (None, True)
                    if row["group_number"] is not None:
                        try:
                            group_number = int(row["group_number"])
                        except (TypeError, ValueError, OverflowError):
                            group_valid = False
                    projected_facts: dict[str, str] = {}
                    fact_keys: dict[str, str] = {}
                    for field in ("article", "manufacturer", "characteristic"):
                        text, valid = self._source_text(facts.get(field))
                        facts_valid = facts_valid and valid
                        projected_facts[field] = text
                        fact_keys[field] = self._identity_fact_key(text)
                    integrity_conflicts: set[str] = set()
                    if not facts_valid:
                        integrity_conflicts.add("malformed_descriptive_provenance")
                    canonical_code_key = self._item_code_key(source_code)
                    if (not code_valid) or (source_code and not canonical_code_key):
                        integrity_conflicts.add("item_code_conflict")
                    canonical_family = normalize_unit_family(raw_unit)
                    if canonical_family:
                        known_unit_families = {canonical_family}
                    else:
                        known_unit_families = set()
                    if (canonical_family or family) and family != (canonical_family or ""):
                        integrity_conflicts.add("unit_family_provenance_invalid")
                    if not item_id or item_id in item_payload or not display_name:
                        raise OneCHistoryReadError("История закупок недоступна")
                    item_payload[item_id] = {
                        "item_id": item_id,
                        "source_item_code": source_code,
                        "display_name": display_name,
                        "raw_unit": raw_unit,
                        "unit_family": family or None,
                        "identity_quality": identity_quality,
                        "group_number": group_number,
                        "facts": projected_facts,
                        "variants": [],
                        "events": [],
                        "valid": all((id_valid, name_valid, unit_valid, quality_valid, code_valid, family_valid, facts_valid, group_valid)),
                        "integrity_conflicts": integrity_conflicts,
                        "known_unit_families": known_unit_families,
                        "descriptive_values": {
                            field: {fact_key} if fact_key else set()
                            for field, fact_key in fact_keys.items()
                        },
                    }

                for row in variant_rows:
                    item_id, item_valid = self._source_text(row["item_id"])
                    if item_id not in item_payload:
                        raise OneCHistoryReadError("История закупок недоступна")
                    texts = [self._source_text(row[key]) for key in ("variant_id", "item_name", "raw_unit", "unit_family", "article", "manufacturer", "characteristic")]
                    source_row, source_row_valid = self._source_row(row["first_source_row"])
                    values = [entry[0] for entry in texts]
                    valid = item_valid and source_row_valid and all(entry[1] for entry in texts)
                    required_variant_fields_present = bool(values[0] and values[1] and values[2])
                    if not required_variant_fields_present or not source_row_valid:
                        valid = False
                        item_payload[item_id]["integrity_conflicts"].add("missing_variant_provenance")
                    if not all(entry[1] for entry in texts):
                        item_payload[item_id]["integrity_conflicts"].add("malformed_descriptive_provenance")
                    variant_family = normalize_unit_family(values[2])
                    declared_variant_family = values[3]
                    if variant_family:
                        item_payload[item_id]["known_unit_families"].add(variant_family)
                    if (variant_family or declared_variant_family) and declared_variant_family != (variant_family or ""):
                        item_payload[item_id]["integrity_conflicts"].add("unit_family_provenance_invalid")
                    for field, value in zip(("article", "manufacturer", "characteristic"), values[4:7]):
                        fact_key = self._identity_fact_key(value)
                        if fact_key:
                            item_payload[item_id]["descriptive_values"][field].add(fact_key)
                    item_payload[item_id]["variants"].append(OneCHistoryVariant(
                        variant_id=values[0], item_name=values[1], raw_unit=values[2],
                        unit_family=values[3] or None, article=values[4], manufacturer=values[5],
                        characteristic=values[6], first_source_row=source_row, provenance_valid=valid,
                    ))
                for payload in item_payload.values():
                    if not payload["variants"]:
                        payload["integrity_conflicts"].add("missing_variant_provenance")

                for row in event_rows:
                    item_id, item_valid = self._source_text(row["item_id"])
                    if item_id not in item_payload:
                        raise OneCHistoryReadError("История закупок недоступна")
                    event_code, event_code_valid = self._source_text(row["item_code"])
                    event_name, event_name_valid = self._source_text(row["item_name"])
                    payload = item_payload[item_id]
                    canonical_code = payload["source_item_code"]
                    canonical_code_key = self._item_code_key(canonical_code)
                    code_consistent = True
                    if not event_code_valid:
                        payload["integrity_conflicts"].add("item_code_conflict")
                        code_consistent = False
                    elif canonical_code_key:
                        event_code_key = self._item_code_key(event_code)
                        if not event_code_key:
                            payload["integrity_conflicts"].add("missing_event_item_code")
                            code_consistent = False
                        elif canonical_code_key != event_code_key:
                            payload["integrity_conflicts"].add("item_code_conflict")
                            code_consistent = False
                    elif event_code:
                        # A weak identity never acquires a code from one of its events.
                        payload["integrity_conflicts"].add("item_code_conflict")
                        code_consistent = False
                    if not event_name:
                        payload["integrity_conflicts"].add("missing_variant_provenance")
                    string_fields = ("event_id", "document_date", "document_type", "counterparty", "price_basis", "raw_unit", "unit_family")
                    text_pairs = {field: self._source_text(row[field]) for field in string_fields}
                    numeric_pairs = [self._decimal(row[field]) for field in (
                        "quantity_decimal", "reported_unit_price_gross_decimal",
                        "effective_unit_price_gross_decimal", "amount_gross_decimal",
                    )]
                    optional_facts, optional_valid = self._json_object(row["optional_facts_json"])
                    if not optional_valid:
                        payload["integrity_conflicts"].add("malformed_descriptive_provenance")
                    currency, currency_valid = self._source_text(optional_facts.get("currency"))
                    event_fact_keys: dict[str, str] = {}
                    event_facts_valid = True
                    for field in ("article", "manufacturer", "characteristic"):
                        fact, fact_valid = self._source_text(optional_facts.get(field))
                        event_facts_valid = event_facts_valid and fact_valid
                        event_fact_keys[field] = self._identity_fact_key(fact)
                        if event_fact_keys[field]:
                            payload["descriptive_values"][field].add(event_fact_keys[field])
                    if not event_facts_valid:
                        payload["integrity_conflicts"].add("malformed_descriptive_provenance")
                    event_unit = text_pairs["raw_unit"][0]
                    event_family = normalize_unit_family(event_unit)
                    declared_event_family = text_pairs["unit_family"][0]
                    if event_family:
                        payload["known_unit_families"].add(event_family)
                    if (event_family or declared_event_family) and declared_event_family != (event_family or ""):
                        payload["integrity_conflicts"].add("unit_family_provenance_invalid")
                    source_row, source_row_valid = self._source_row(row["source_row"])
                    event_valid = (
                        item_valid and event_code_valid and event_name_valid and bool(event_name)
                        and code_consistent and source_row_valid and optional_valid and currency_valid
                        and event_facts_valid and all(pair[1] for pair in text_pairs.values())
                    )
                    event_id = text_pairs["event_id"][0]
                    if not event_id:
                        event_valid = False
                    date_text = text_pairs["document_date"][0]
                    try:
                        if date_text:
                            datetime.fromisoformat(date_text)
                    except ValueError:
                        event_valid = False
                    price_usable = row["price_usable"] == 1 and all(pair[1] for pair in numeric_pairs)
                    if price_usable and numeric_pairs[2][0] is None:
                        price_usable = False
                    item_payload[item_id]["events"].append(OneCHistoryEvent(
                        event_id=event_id,
                        item_id=item_id,
                        document_date=date_text,
                        document_type=text_pairs["document_type"][0],
                        counterparty=text_pairs["counterparty"][0],
                        quantity=numeric_pairs[0][0],
                        reported_unit_price_gross=numeric_pairs[1][0],
                        effective_unit_price_gross=numeric_pairs[2][0],
                        amount_gross=numeric_pairs[3][0],
                        price_usable=price_usable,
                        price_basis=text_pairs["price_basis"][0],
                        raw_unit=text_pairs["raw_unit"][0],
                        unit_family=text_pairs["unit_family"][0] or None,
                        currency=currency,
                        source_row=source_row,
                        provenance_valid=event_valid,
                        numeric_values_valid=all(pair[1] for pair in numeric_pairs),
                    ))

                expected_items = metadata.get("item_count")
                expected_events = metadata.get("event_count")
                if (isinstance(expected_items, int) and expected_items != len(item_payload)) or (
                    isinstance(expected_events, int) and expected_events != len(event_rows)
                ):
                    raise OneCHistoryReadError("История закупок недоступна")

                for payload in item_payload.values():
                    if len(payload["known_unit_families"]) > 1:
                        payload["integrity_conflicts"].add("unit_family_conflict")
                    conflict_codes = {
                        "article": "article_conflict",
                        "manufacturer": "manufacturer_conflict",
                        "characteristic": "characteristic_conflict",
                    }
                    for field, code in conflict_codes.items():
                        if len(payload["descriptive_values"][field]) > 1:
                            payload["integrity_conflicts"].add(code)

                items = tuple(
                    OneCHistoryItem(
                        item_id=payload["item_id"],
                        source_item_code=payload["source_item_code"],
                        display_name=payload["display_name"],
                        raw_unit=payload["raw_unit"],
                        unit_family=payload["unit_family"],
                        identity_quality=payload["identity_quality"],
                        group_number=payload["group_number"],
                        article=payload["facts"]["article"],
                        manufacturer=payload["facts"]["manufacturer"],
                        characteristic=payload["facts"]["characteristic"],
                        variants=tuple(payload["variants"]),
                        events=tuple(payload["events"]),
                        provenance_valid=payload["valid"],
                        integrity_conflicts=tuple(sorted(payload["integrity_conflicts"])),
                    )
                    for payload in item_payload.values()
                )
                return OneCHistoryCatalogSnapshot(version=version, items=items)
            except OneCHistoryReadError:
                raise
            except Exception as exc:
                raise OneCHistoryReadError("История закупок недоступна") from exc
            finally:
                if connection is not None:
                    connection.close()

    def public_status(self) -> dict:
        status = self._read_json(self.status_path, {})
        return {
            "active_import": self.active_metadata(),
            "last_attempt": status.get("last_attempt") if isinstance(status, dict) else None,
            "profiles": [item.model_dump(mode="json") for item in self.profiles()],
        }

    def build_staging_snapshot(
        self,
        parsed: ParsedWorkbook,
        *,
        profile_id: str | None,
        mapping_provenance: dict | None = None,
        semantic_import_fingerprint: str,
    ) -> Path:
        staging = self.root / f".history-{parsed.file_sha256[:12]}-{os.getpid()}-{threading.get_ident()}.staging.sqlite3"
        staging.unlink(missing_ok=True)
        metadata = {
            "sha256": parsed.file_sha256,
            "original_filename": parsed.filename,
            "sheet_name": parsed.sheet_name,
            "period_start": parsed.period_start,
            "period_end": parsed.period_end,
            "imported_at": datetime.now(timezone.utc).isoformat(),
            "parser_version": PARSER_VERSION,
            "mapping_profile_id": profile_id,
            "semantic_import_fingerprint": semantic_import_fingerprint,
            "identity_quality": "stable_code_present" if parsed.missing_code_count == 0 else "degraded_missing_stable_code",
            "price_basis": "gross_including_vat",
            "item_count": parsed.item_count,
            "group_count": parsed.group_count,
            "physical_row_count": parsed.physical_row_count,
            "distinct_counterparty_count": parsed.distinct_counterparty_count,
            "unit_vocabulary_count": parsed.unit_vocabulary_count,
            "event_count": len(parsed.events),
            "usable_price_event_count": sum(event.price_usable for event in parsed.events),
            "supplier_missing_count": parsed.supplier_missing_count,
            "unusable_price_count": parsed.unusable_price_count,
            "repeated_display_label_count": parsed.repeated_display_label_count,
            "normalized_display_collision_count": parsed.normalized_display_collision_count,
            "warning_count": len(parsed.warnings),
            "skipped_row_count": parsed.skipped_row_count,
            "warnings": parsed.warnings,
            "document_type_counts": parsed.document_type_counts,
            "document_type_other_event_count": parsed.document_type_other_event_count,
            "layout_type": parsed.layout_type,
            "header_signature": parsed.header_signature,
            "mapping_provenance": mapping_provenance or {},
        }
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect(staging)
            with connection:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=FULL")
                connection.executescript(
                    """
                    CREATE TABLE import_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE nomenclature_items (
                        item_id TEXT PRIMARY KEY,
                        source_item_code TEXT,
                        display_name TEXT NOT NULL,
                        normalized_name TEXT NOT NULL,
                        raw_unit TEXT NOT NULL,
                        unit_family TEXT,
                        identity_quality TEXT NOT NULL,
                        group_number INTEGER,
                        descriptive_facts_json TEXT NOT NULL
                    );
                    CREATE INDEX idx_one_c_item_code ON nomenclature_items(source_item_code);
                    CREATE INDEX idx_one_c_item_name ON nomenclature_items(normalized_name);
                    CREATE TABLE nomenclature_descriptive_variants (
                        variant_id TEXT PRIMARY KEY,
                        item_id TEXT NOT NULL REFERENCES nomenclature_items(item_id),
                        item_name TEXT NOT NULL,
                        raw_unit TEXT NOT NULL,
                        unit_family TEXT,
                        article TEXT,
                        manufacturer TEXT,
                        characteristic TEXT,
                        first_source_row INTEGER NOT NULL
                    );
                    CREATE INDEX idx_one_c_variant_name ON nomenclature_descriptive_variants(item_name);
                    CREATE INDEX idx_one_c_variant_article ON nomenclature_descriptive_variants(article);
                    CREATE TABLE purchase_events (
                        event_id TEXT PRIMARY KEY,
                        item_id TEXT NOT NULL REFERENCES nomenclature_items(item_id),
                        item_code TEXT,
                        item_name TEXT NOT NULL,
                        raw_unit TEXT NOT NULL,
                        unit_family TEXT,
                        document_date TEXT,
                        document_type TEXT NOT NULL,
                        document_reference TEXT NOT NULL,
                        counterparty TEXT NOT NULL,
                        contract TEXT NOT NULL,
                        quantity_decimal TEXT,
                        reported_unit_price_gross_decimal TEXT,
                        effective_unit_price_gross_decimal TEXT,
                        amount_gross_decimal TEXT,
                        price_usable INTEGER NOT NULL CHECK(price_usable IN (0, 1)),
                        price_basis TEXT NOT NULL,
                        source_sheet TEXT NOT NULL,
                        source_row INTEGER NOT NULL,
                        parser_version INTEGER NOT NULL,
                        optional_facts_json TEXT NOT NULL,
                        source_facts_json TEXT NOT NULL
                    );
                    CREATE INDEX idx_one_c_event_item ON purchase_events(item_id);
                    CREATE INDEX idx_one_c_event_date ON purchase_events(document_date);
                    CREATE INDEX idx_one_c_event_type ON purchase_events(document_type);
                    """
                )
                with connection:
                    connection.executemany(
                        "INSERT INTO import_metadata(key,value) VALUES (?,?)",
                        [(key, json.dumps(value, ensure_ascii=False, separators=(",", ":"))) for key, value in metadata.items()],
                    )
                    items: dict[str, dict] = {}
                    for event in parsed.events:
                        items.setdefault(event.item_key, {
                            "item_id": event.item_key,
                            "source_item_code": event.item_code,
                            "display_name": event.item_name,
                            "normalized_name": " ".join(event.item_name.casefold().replace("ё", "е").split()),
                            "raw_unit": event.raw_unit,
                            "unit_family": event.unit_family,
                            "identity_quality": event.identity_quality,
                            "group_number": event.group_number,
                            "descriptive_facts_json": json.dumps(event.optional_facts, ensure_ascii=False, separators=(",", ":")),
                        })
                    connection.executemany(
                        """INSERT INTO nomenclature_items(
                            item_id,source_item_code,display_name,normalized_name,raw_unit,unit_family,
                            identity_quality,group_number,descriptive_facts_json
                        ) VALUES (?,?,?,?,?,?,?,?,?)""",
                        [tuple(item[key] for key in (
                            "item_id", "source_item_code", "display_name", "normalized_name", "raw_unit",
                            "unit_family", "identity_quality", "group_number", "descriptive_facts_json",
                        )) for item in items.values()],
                    )
                    variants: dict[str, tuple] = {}
                    for event in parsed.events:
                        facts = (
                            event.item_key, event.item_name, event.raw_unit, event.unit_family,
                            event.optional_facts.get("article"), event.optional_facts.get("manufacturer"),
                            event.optional_facts.get("characteristic"),
                        )
                        variant_id = hashlib.sha256(
                            json.dumps(facts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                        ).hexdigest()
                        variants.setdefault(variant_id, (variant_id, *facts, event.source_row))
                    connection.executemany(
                        """INSERT INTO nomenclature_descriptive_variants(
                            variant_id,item_id,item_name,raw_unit,unit_family,article,manufacturer,characteristic,first_source_row
                        ) VALUES (?,?,?,?,?,?,?,?,?)""",
                        list(variants.values()),
                    )
                    connection.executemany(
                        """INSERT INTO purchase_events(
                            event_id,item_id,item_code,item_name,raw_unit,unit_family,document_date,document_type,
                            document_reference,counterparty,contract,quantity_decimal,
                            reported_unit_price_gross_decimal,effective_unit_price_gross_decimal,
                            amount_gross_decimal,price_usable,price_basis,source_sheet,source_row,
                            parser_version,optional_facts_json,source_facts_json
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        [(
                            f"{parsed.file_sha256}:{parsed.sheet_name}:{event.source_row}", event.item_key,
                            event.item_code, event.item_name, event.raw_unit, event.unit_family,
                            event.document_date, event.document_type, event.document_reference,
                            event.counterparty, event.contract, event.quantity,
                            event.reported_unit_price_gross, event.effective_unit_price_gross,
                            event.amount_gross, int(event.price_usable), "gross_including_vat",
                            parsed.sheet_name, event.source_row, PARSER_VERSION,
                            json.dumps(event.optional_facts, ensure_ascii=False, separators=(",", ":")),
                            json.dumps(event.source_facts, ensure_ascii=False, separators=(",", ":")),
                        ) for event in parsed.events],
                    )
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise sqlite3.IntegrityError("staging database has foreign-key violations")
                check = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if check != "ok":
                    raise sqlite3.IntegrityError("staging database failed integrity check")
                if connection.execute("SELECT COUNT(*) FROM purchase_events").fetchone()[0] != len(parsed.events):
                    raise sqlite3.IntegrityError("staging event count mismatch")
            connection.close()
            connection = None
            descriptor = os.open(staging, os.O_RDWR)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return staging
        except Exception:
            if connection is not None:
                connection.close()
            staging.unlink(missing_ok=True)
            for sidecar in staging.parent.glob(staging.name + "-*"):
                sidecar.unlink(missing_ok=True)
            raise

    def activate(self, staging: Path) -> list[str]:
        with self._lock:
            os.replace(staging, self.database_path)
            warnings = []
            try:
                self._fsync_directory()
            except Exception:
                # os.replace is the commit point. A durability bookkeeping
                # failure after it must never be reported as if old data won.
                warnings.append("Не удалось подтвердить синхронизацию каталога после активации снимка.")
            return warnings

    def _fsync_directory(self) -> None:
        if os.name == "nt":
            return
        directory_fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


__all__ = ["OneCHistoryRepository"]
