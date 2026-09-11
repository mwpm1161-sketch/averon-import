from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_RUN_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_SAFE_DECISIONS = {
    "MATCH",
    "LIKELY_MATCH",
    "ALTERNATIVE",
    "REVIEW",
    "WITHOUT_OFFERS",
}


class SourcingRunHistory:
    """Workspace-owned, sanitized audit records for document sourcing runs."""

    retention_limit = 50

    def __init__(self, root: Path, *, retention_limit: int | None = None):
        self.root = Path(root)
        self.retention_limit = max(1, int(retention_limit or self.retention_limit))

    @staticmethod
    def new_run_id() -> str:
        return uuid.uuid4().hex

    def write_completed(
        self,
        *,
        run_id: str,
        document_id: str,
        created_at: str,
        completed_at: str,
        provider_key: str,
        provider_label: str,
        catalog_version: str,
        result: Any,
        row_telemetry: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = _result_dict(result)
        record = {
            "run_id": _safe_run_id(run_id),
            "document_id": str(document_id),
            "created_at": str(created_at),
            "completed_at": str(completed_at),
            "status": "completed",
            "provider_key": _safe_text(provider_key, 100),
            "provider_label": _safe_text(provider_label, 180),
            "catalog_version": _safe_text(catalog_version, 120),
            "positions_total": _safe_int(payload.get("positions_total")),
            "positions_processed": _safe_int(payload.get("positions_processed")),
            "positions_matched": _safe_int(payload.get("positions_matched")),
            "positions_alternatives": _safe_int(payload.get("positions_alternatives")),
            "positions_review": _safe_int(payload.get("positions_review")),
            "positions_without_offers": _safe_int(payload.get("positions_without_offers")),
            "confirmed_total": payload.get("confirmed_total"),
            "confirmed_totals": _safe_mapping(payload.get("confirmed_totals")),
            "confirmed_currency": _safe_optional_text(payload.get("confirmed_currency"), 16),
            "alternative_total": payload.get("alternative_total"),
            "alternative_totals": _safe_mapping(payload.get("alternative_totals")),
            "alternative_currency": _safe_optional_text(payload.get("alternative_currency"), 16),
            "matched_unpriced_count": _safe_int(payload.get("matched_unpriced_count")),
            "alternative_unpriced_count": _safe_int(payload.get("alternative_unpriced_count")),
            "unresolved_count": _safe_int(payload.get("unresolved_count")),
            "timings": _safe_timings(payload.get("timings")),
            "rows": [_sanitize_row(item) for item in row_telemetry],
        }
        return self._persist(record)

    def write_failed(
        self,
        *,
        run_id: str,
        document_id: str,
        created_at: str,
        provider_key: str,
        provider_label: str,
        catalog_version: str,
        positions_total: int,
        progress_current: int,
        progress_total: int,
        exc: Exception,
    ) -> dict[str, Any]:
        category, message = _safe_error(exc)
        record = {
            "run_id": _safe_run_id(run_id),
            "document_id": str(document_id),
            "created_at": str(created_at),
            "completed_at": None,
            "status": "failed",
            "provider_key": _safe_text(provider_key, 100),
            "provider_label": _safe_text(provider_label, 180),
            "catalog_version": _safe_text(catalog_version, 120),
            "positions_total": _safe_int(positions_total),
            "positions_processed": _safe_int(progress_current),
            "positions_matched": 0,
            "positions_alternatives": 0,
            "positions_review": 0,
            "positions_without_offers": 0,
            "confirmed_total": None,
            "confirmed_totals": {},
            "confirmed_currency": None,
            "alternative_total": None,
            "alternative_totals": {},
            "alternative_currency": None,
            "matched_unpriced_count": 0,
            "alternative_unpriced_count": 0,
            "unresolved_count": 0,
            "timings": {},
            "progress_current": _safe_int(progress_current),
            "progress_total": _safe_int(progress_total),
            "error_category": category,
            "error_message": message,
            "rows": [],
        }
        return self._persist(record)

    def list_records(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        records: list[dict[str, Any]] = []
        try:
            paths = list(self.root.glob("*.json"))
        except OSError:
            return []
        for path in paths:
            record = self._read(path)
            if record is not None:
                records.append(record)
        records.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return records

    def list_public(self) -> list[dict[str, Any]]:
        return [_without_rows(record) for record in self.list_records()]

    def get(self, run_id: str) -> dict[str, Any] | None:
        if not _RUN_ID_RE.fullmatch(str(run_id)):
            return None
        return self._read(self.root / f"{run_id}.json")

    def _persist(self, record: dict[str, Any]) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{record['run_id']}.json"
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
        self._prune()
        return record

    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        return value if isinstance(value, dict) and _RUN_ID_RE.fullmatch(str(value.get("run_id") or "")) else None

    def _prune(self) -> None:
        valid: list[tuple[str, Path]] = []
        try:
            paths = list(self.root.glob("*.json"))
        except OSError:
            return
        for path in paths:
            record = self._read(path)
            if record is not None:
                valid.append((str(record.get("created_at") or ""), path))
        valid.sort(key=lambda item: item[0], reverse=True)
        for _, path in valid[self.retention_limit:]:
            try:
                path.unlink()
            except OSError:
                continue


def _result_dict(result: Any) -> dict[str, Any]:
    if hasattr(result, "model_dump"):
        value = result.model_dump(mode="json")
    elif isinstance(result, dict):
        value = result
    else:
        return {}
    return value if isinstance(value, dict) else {}


def _sanitize_row(value: dict[str, Any]) -> dict[str, Any]:
    match = value.get("match") if isinstance(value.get("match"), dict) else {}
    provenance = value.get("understanding_provenance")
    if not isinstance(provenance, dict):
        provenance = {}
    return {
        "source_row_id": _safe_text(value.get("source_row_id"), 160),
        "source_page": _safe_int_or_none(value.get("source_page")),
        "source_row": _safe_int_or_none(value.get("source_row")),
        "intent_fingerprint": _safe_text(value.get("intent_fingerprint"), 128),
        "ai_mode": _safe_text(value.get("ai_mode"), 32),
        "understanding_cache_hit": bool(value.get("understanding_cache_hit")),
        "understanding_provenance": {
            "kind": _safe_text(provenance.get("kind"), 80),
            "provider": _safe_text(provenance.get("provider"), 100),
            "model": _safe_text(provenance.get("model"), 180),
            "parser_revision": _safe_text(provenance.get("parser_revision"), 80),
            "latency_ms": _safe_float_or_none(provenance.get("latency_ms")),
        },
        "search_cache_hit": bool(value.get("search_cache_hit")),
        "decision": _safe_decision(value.get("decision")),
        "recommended_offer_id": _safe_optional_text(value.get("recommended_offer_id"), 180),
        "review_candidate_offer_id": _safe_optional_text(value.get("review_candidate_offer_id"), 180),
        "matched_attributes": _safe_text_list(value.get("matched_attributes")),
        "missing_attributes": _safe_text_list(value.get("missing_attributes")),
        "conflicting_attributes": _safe_text_list(value.get("conflicting_attributes")),
        "preferred_differences": _safe_text_list(value.get("preferred_differences")),
        **({"match": {
            "matched_attributes": _safe_text_list(match.get("matched_attributes")),
            "missing_attributes": _safe_text_list(match.get("missing_attributes")),
            "conflicting_attributes": _safe_text_list(match.get("conflicting_attributes")),
        }} if match else {}),
    }


def _without_rows(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "rows"}


def _safe_run_id(value: str) -> str:
    text = str(value or "")
    return text if _RUN_ID_RE.fullmatch(text) else uuid.uuid4().hex


def _safe_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _safe_optional_text(value: Any, limit: int) -> str | None:
    text = _safe_text(value, limit)
    return text or None


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _safe_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {_safe_text(key, 40): child for key, child in value.items() if _safe_text(key, 40)}


def _safe_timings(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, float] = {}
    for key, child in value.items():
        number = _safe_float_or_none(child)
        if number is not None:
            result[_safe_text(key, 80)] = number
    return result


def _safe_text_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [_safe_text(item, 160) for item in value if _safe_text(item, 160)][:20]


def _safe_decision(value: Any) -> str:
    decision = _safe_text(value, 40).upper()
    return decision if decision in _SAFE_DECISIONS else "REVIEW"


def _safe_error(exc: Exception) -> tuple[str, str]:
    category = _safe_text(getattr(exc, "category", "") or "sourcing_error", 80).lower()
    if any(marker in category for marker in ("api-key", "authorization", "secret", "token", "password")):
        category = "sourcing_error"
    message = " ".join(str(exc).split())
    lowered = message.casefold()
    if not message or len(message) > 240 or any(
        marker in lowered for marker in ("api-key", "authorization", "secret", "token", "password")
    ):
        message = "Задание подбора не выполнено"
    return category, message[:240]
