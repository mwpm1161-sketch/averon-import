from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from typing import Any

from averon_import.services.one_c_history.models import ImportProfile, utc_now
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
