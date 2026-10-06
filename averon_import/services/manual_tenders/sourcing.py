from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .repository import (
    MAX_TENDER_RUN_BYTES,
    MAX_TENDER_RUNS_PER_WORKSPACE,
    TenderWorkspaceError,
    TenderWorkspaceRepository,
)
from .history_fuzzy_eligibility import (
    fuzzy_confirmation_eligibility_reason,
)
from .history_projection import (
    HISTORY_TEXT_POOL_KEY,
    MAX_COUNTERPARTY_DISPLAY_BYTES,
    MAX_WAREHOUSE_DISPLAY_BYTES,
    bounded_utf8_text,
    encode_history_candidate_rows,
    pending_history_candidate_v2,
)

_RUN_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_SAFE_CODE_RE = re.compile(r"^[A-Z0-9_-]{1,80}$")
_TERMINAL = {"completed", "failed", "interrupted"}
_PRICE_PROVENANCE_FIELDS = {
    "etm_ipro": (
        "source", "catalog_version", "source_item_id", "price_field", "price_status",
    ),
    "one_c_history": (
        "source", "source_kind", "snapshot_version", "history_item_id",
        "selected_event_id", "purchase_date", "price_basis",
        "effective_unit_price_gross", "currency_basis", "unit_family",
        "normalizer_revision", "normalized_name_signature", "warehouse",
        "warehouse_truncated", "counterparty", "counterparty_truncated",
    ),
    "lemana_b2b": ("source", "product_item", "mirror_revision"),
}
_PRICE_PROVENANCE_STRING_LIMITS = {
    "source": 40,
    "catalog_version": 120,
    "source_item_id": 180,
    "price_field": 40,
    "price_status": 80,
    "source_kind": 80,
    "snapshot_version": 120,
    "history_item_id": 180,
    "selected_event_id": 180,
    "purchase_date": 40,
    "price_basis": 80,
    "effective_unit_price_gross": 80,
    "currency_basis": 80,
    "unit_family": 80,
    "normalizer_revision": 80,
    "normalized_name_signature": 64,
    "warehouse": MAX_WAREHOUSE_DISPLAY_BYTES,
    "counterparty": MAX_COUNTERPARTY_DISPLAY_BYTES,
    "product_item": 180,
    "mirror_revision": 120,
}
_OMIT_PROVENANCE = object()
_MAX_HISTORY_REVIEW_CANDIDATES_PER_ROW = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TenderSourcingRowAdapter:
    """Convert immutable server-owned tender rows to existing sourcing input."""

    @staticmethod
    def convert(source: dict[str, Any]) -> dict[str, Any]:
        if source.get("row_type") != "item":
            raise TenderWorkspaceError("Выбрана строка, которая не является позицией.", 400, "TENDER_SOURCE_ROW_NOT_ITEM")
        article = str(source.get("article") or "").strip()
        quantity = source.get("quantity") if source.get("quantity_trusted") is True else ""
        return {
            "source_row_id": str(source.get("source_row_id") or ""),
            "row_type": "item",
            "selected": True,
            "name": str(source.get("name") or ""),
            "type_mark": str(source.get("model") or ""),
            "manufacturer": str(source.get("manufacturer") or ""),
            "article": article,
            "quantity": str(quantity or ""),
            "quantity_trusted": source.get("quantity_trusted") is True,
            "unit": str(source.get("raw_unit") or ""),
        }

    @classmethod
    def selected_rows(cls, rows: list[dict[str, Any]], selected_ids: list[str]) -> list[dict[str, Any]]:
        by_id: dict[str, dict[str, Any]] = {}
        for row in rows:
            row_id = str(row.get("source_row_id") or "")
            if not row_id or row_id in by_id:
                raise TenderWorkspaceError("Строки тендера повреждены.", 409, "TENDER_WORKSPACE_CORRUPT")
            by_id[row_id] = row
        missing = [row_id for row_id in selected_ids if row_id not in by_id]
        if missing:
            raise TenderWorkspaceError("Одна или несколько выбранных строк не найдены в тендере.", 400, "TENDER_SOURCE_ROW_UNKNOWN")
        return [cls.convert(by_id[row_id]) for row_id in selected_ids]


class TenderSourcingRunStore:
    """Atomic, bounded, workspace-local durable sourcing run records."""

    def __init__(self, repository: TenderWorkspaceRepository):
        self.repository = repository
        self._lock = threading.RLock()
        self._recover_running_runs()

    @staticmethod
    def _path(workspace_path: Path, run_id: str) -> Path:
        if not _RUN_ID_RE.fullmatch(str(run_id or "")):
            raise TenderWorkspaceError("Запуск подбора не найден.")
        return workspace_path / "runs" / f"{run_id}.json"

    @staticmethod
    def _read_path(path: Path, *, tender_id: str | None = None) -> dict[str, Any]:
        try:
            if path.stat().st_size > MAX_TENDER_RUN_BYTES:
                raise ValueError("run exceeds size limit")
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("schema_version") != 1:
                raise ValueError("invalid run shape")
            if tender_id is not None and value.get("tender_id") != tender_id:
                raise TenderWorkspaceError("Запуск подбора не найден.")
            return value
        except TenderWorkspaceError:
            raise
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise TenderWorkspaceError("Данные подбора повреждены.", 409, "TENDER_RUN_CORRUPT") from exc

    @staticmethod
    def _atomic_write(path: Path, value: dict[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        encoded = payload.encode("utf-8")
        if len(encoded) > MAX_TENDER_RUN_BYTES:
            raise TenderWorkspaceError("Результат подбора превышает допустимый объём.", 413, "TENDER_RUN_TOO_LARGE")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}.{uuid.uuid4().hex}.", suffix=".tmp", dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _recover_running_runs(self) -> None:
        with self._lock:
            for workspace_path in self.repository._workspace_dirs():
                runs = workspace_path / "runs"
                if not runs.is_dir():
                    continue
                for path in runs.glob("[a-f0-9]" * 32 + ".json"):
                    try:
                        run = self._read_path(path, tender_id=workspace_path.name)
                        if run.get("status") != "running":
                            continue
                        run.update({
                            "status": "interrupted",
                            "completed_at": _now(),
                            "failure": {
                                "code": "SERVER_RESTART",
                                "message": "Подбор был прерван перезапуском сервера. Запустите его повторно.",
                            },
                        })
                        self._atomic_write(path, run)
                    except (OSError, TenderWorkspaceError):
                        continue
                self._prune_terminal(workspace_path)

    def create_running(
        self,
        workspace_path: Path,
        metadata: dict[str, Any],
        *,
        source_mode: str,
        provider: str | None,
        selected_ids: list[str],
        history_catalog_version: str | None,
    ) -> dict[str, Any]:
        self.repository.reserve_tender_run_storage(workspace_path)
        run_id = uuid.uuid4().hex
        now = _now()
        run = {
            "schema_version": 1,
            "run_id": run_id,
            "tender_id": metadata["tender_id"],
            "source_sha256": metadata["source_sha256"],
            "workspace_revision": int(metadata["revision"]),
            "status": "running",
            "source_mode": source_mode,
            "provider_request": provider,
            "selected_source_row_ids": list(selected_ids),
            "created_at": now,
            "started_at": now,
            "completed_at": None,
            "catalog_version": None,
            "history_catalog_version": history_catalog_version,
            "summary": {
                "positions_total": len(selected_ids),
                "positions_processed": 0,
                "positions_matched": 0,
                "positions_review": 0,
                "positions_without_offers": 0,
            },
            "rows": [],
            "failure": None,
        }
        with self._lock:
            self._atomic_write(self._path(workspace_path, run_id), run)
        return run

    def complete(
        self,
        workspace_path: Path,
        run_id: str,
        *,
        summary: dict[str, Any],
        catalog_version: str | None,
        history_catalog_version: str | None,
        rows: list[dict[str, Any]],
    ) -> None:
        path = self._path(workspace_path, run_id)
        with self._lock:
            run = self._read_path(path)
            if run.get("status") != "running":
                raise TenderWorkspaceError("Запуск подбора уже завершён.", 409, "TENDER_RUN_IMMUTABLE")
            run.update({
                "status": "completed", "completed_at": _now(),
                "catalog_version": catalog_version,
                "history_catalog_version": history_catalog_version,
                "summary": summary, "failure": None,
            })
            try:
                encoded_rows, text_pool = encode_history_candidate_rows(rows)
            except (TypeError, ValueError, UnicodeEncodeError) as exc:
                raise TenderWorkspaceError(
                    "Результат подбора содержит повреждённые данные истории.",
                    409, "TENDER_RESULT_INVALID",
                ) from exc
            run["rows"] = encoded_rows
            if text_pool:
                run[HISTORY_TEXT_POOL_KEY] = text_pool
            self._atomic_write(path, run)
            self._prune_terminal(workspace_path)

    def fail(
        self,
        workspace_path: Path,
        run_id: str,
        *,
        code: str,
        progress_current: int,
        progress_total: int,
    ) -> None:
        path = self._path(workspace_path, run_id)
        safe_code = code if _SAFE_CODE_RE.fullmatch(code) else "SOURCING_FAILED"
        with self._lock:
            try:
                run = self._read_path(path)
            except TenderWorkspaceError:
                return
            if run.get("status") != "running":
                return
            run.update({
                "status": "failed", "completed_at": _now(),
                "summary": {
                    "positions_total": max(0, int(progress_total)),
                    "positions_processed": max(0, min(int(progress_current), int(progress_total))),
                    "positions_matched": 0, "positions_review": 0,
                    "positions_without_offers": 0,
                },
                "rows": [],
                "failure": {
                    "code": safe_code,
                    "message": "Подбор не удалось завершить. Проверьте тендер и повторите попытку.",
                },
            })
            try:
                self._atomic_write(path, run)
                self._prune_terminal(workspace_path)
            except TenderWorkspaceError:
                pass

    def _prune_terminal(self, workspace_path: Path) -> None:
        runs = workspace_path / "runs"
        terminal: list[tuple[str, Path]] = []
        if not runs.is_dir():
            return
        for path in runs.glob("[a-f0-9]" * 32 + ".json"):
            try:
                record = self._read_path(path, tender_id=workspace_path.name)
                if record.get("status") in _TERMINAL:
                    terminal.append((str(record.get("completed_at") or record.get("created_at") or ""), path))
            except TenderWorkspaceError:
                continue
        terminal.sort(key=lambda item: item[0])
        for _, path in terminal[:-MAX_TENDER_RUNS_PER_WORKSPACE]:
            try:
                path.unlink(missing_ok=True)
                (workspace_path / "history-decisions" / path.name).unlink(missing_ok=True)
            except OSError:
                continue

    def list_public(self, workspace_path: Path, tender_id: str) -> dict[str, Any]:
        runs = workspace_path / "runs"
        items: list[dict[str, Any]] = []
        if runs.is_dir():
            for path in runs.glob("[a-f0-9]" * 32 + ".json"):
                try:
                    run = self._read_path(path, tender_id=tender_id)
                except TenderWorkspaceError:
                    continue
                summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
                items.append({
                    "run_id": run["run_id"], "tender_id": tender_id,
                    "status": run.get("status"), "source_mode": run.get("source_mode"),
                    "created_at": run.get("created_at"), "started_at": run.get("started_at"),
                    "completed_at": run.get("completed_at"), "catalog_version": run.get("catalog_version"),
                    "history_catalog_version": run.get("history_catalog_version"),
                    "summary": summary, "failure": run.get("failure"),
                })
        items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return {"runs": items[:MAX_TENDER_RUNS_PER_WORKSPACE]}

    def get_public(self, workspace_path: Path, tender_id: str, run_id: str) -> dict[str, Any]:
        run = self._read_path(self._path(workspace_path, run_id), tender_id=tender_id)
        from .history_decisions import _expand_review_candidate
        from .history_projection import decode_history_candidate_v2

        pool = run.get(HISTORY_TEXT_POOL_KEY, [])
        if (
            not isinstance(pool, list) or any(not isinstance(item, str) for item in pool)
            or pool != sorted(set(pool))
        ):
            raise TenderWorkspaceError("Данные подбора повреждены.", 409, "TENDER_RUN_CORRUPT")
        for row in run.get("rows", []):
            if not isinstance(row, dict) or not isinstance(row.get("history_review_candidates", []), list):
                raise TenderWorkspaceError("Данные подбора повреждены.", 409, "TENDER_RUN_CORRUPT")
            expanded_candidates = []
            for candidate in row.get("history_review_candidates", []):
                try:
                    compact = decode_history_candidate_v2(candidate, pool)
                except (TypeError, ValueError) as exc:
                    raise TenderWorkspaceError("Данные подбора повреждены.", 409, "TENDER_RUN_CORRUPT") from exc
                if compact is not None:
                    candidate = _expand_review_candidate(compact)
                    if not candidate:
                        raise TenderWorkspaceError("Данные подбора повреждены.", 409, "TENDER_RUN_CORRUPT")
                expanded_candidates.append(candidate)
            row["history_review_candidates"] = expanded_candidates
        run.pop(HISTORY_TEXT_POOL_KEY, None)
        return run


def _safe_match(match: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(match, dict):
        return None
    safe = {
        key: match.get(key)
        for key in ("decision", "rank", "matched_attributes", "supporting_attributes", "conflicting_attributes", "missing_attributes")
        if key in match
    }
    offer = match.get("offer") if isinstance(match.get("offer"), dict) else {}
    if isinstance(offer.get("offer_id"), str):
        safe["offer_id"] = offer["offer_id"][:180]
    return safe


def _safe_history_review_match(match: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key in ("decision",):
        value = match.get(key)
        if isinstance(value, str):
            safe[key] = value[:40]
    offer = match.get("offer") if isinstance(match.get("offer"), dict) else {}
    offer_id = offer.get("offer_id")
    if isinstance(offer_id, str):
        safe["offer_id"] = offer_id[:180]
    for key in ("matched_attributes", "supporting_attributes", "conflicting_attributes", "missing_attributes"):
        values = match.get(key)
        if isinstance(values, list):
            safe[key] = [item[:80] for item in values[:20] if isinstance(item, str)]
    return safe


def _safe_provenance_value(key: str, value: Any) -> Any:
    """Keep one bounded primitive from a known provider provenance contract."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            return _OMIT_PROVENANCE
        rendered = str(value)
        return rendered if len(rendered) <= _PRICE_PROVENANCE_STRING_LIMITS[key] else _OMIT_PROVENANCE
    if isinstance(value, str):
        if key in {"warehouse", "counterparty"}:
            value, _truncated = bounded_utf8_text(value, _PRICE_PROVENANCE_STRING_LIMITS[key])
        return value[:_PRICE_PROVENANCE_STRING_LIMITS[key]]
    if isinstance(value, int):
        return value if len(str(value)) <= 32 else _OMIT_PROVENANCE
    if isinstance(value, float):
        return value if math.isfinite(value) else _OMIT_PROVENANCE
    return _OMIT_PROVENANCE


def _safe_price_provenance(offer: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(offer, dict):
        return {}
    provider = offer.get("provider")
    fields = _PRICE_PROVENANCE_FIELDS.get(provider, ()) if isinstance(provider, str) else ()
    provenance = offer.get("data_provenance")
    if not fields or not isinstance(provenance, dict):
        return {}
    # Provider identity is a fixed part of each supported contract, rather
    # than an arbitrary value supplied under an allowlisted key.
    if provenance.get("source") != provider:
        return {}
    safe: dict[str, Any] = {}
    for key in fields:
        if key not in provenance:
            continue
        if key in {"warehouse", "counterparty"}:
            text, truncated = bounded_utf8_text(provenance[key], _PRICE_PROVENANCE_STRING_LIMITS[key])
            if text:
                safe[key] = text
            if truncated or provenance.get(f"{key}_truncated") is True:
                safe[f"{key}_truncated"] = True
            continue
        if key in {"warehouse_truncated", "counterparty_truncated"}:
            if provenance.get(key) is True:
                safe[key] = True
            continue
        value = _safe_provenance_value(key, provenance[key])
        if value is not _OMIT_PROVENANCE:
            safe[key] = value
    return safe


def _has_compact_fuzzy_history_provenance(
    offer: dict[str, Any], safe_offer: dict[str, Any], safe_provenance: dict[str, Any],
) -> bool:
    """Only encode fixed fuzzy-v1 tokens when they match source facts."""
    provenance = offer.get("data_provenance")
    if not isinstance(provenance, dict):
        return False
    required = {
        "source": "one_c_history",
        "source_kind": "historical_purchase",
        "price_basis": "gross_including_vat",
    }
    return (
        offer.get("provider") == "one_c_history"
        and safe_offer.get("provider") == "one_c_history"
        and all(provenance.get(key) == value for key, value in required.items())
        and all(safe_provenance.get(key) == value for key, value in required.items())
    )


def _compact_fuzzy_projection_candidate(
    candidate: dict[str, Any],
    candidate_match: dict[str, Any],
    source: dict[str, Any],
    route: dict[str, Any],
    retrieval_rank: Any,
) -> dict[str, Any] | None:
    """Build a compact fuzzy slot only when its static confirmation evidence is sound."""
    safe_candidate: dict[str, Any] = {}
    for key, limit in (
        ("offer_id", 180), ("provider", 40), ("source_item_id", 180), ("title", 500),
        ("article", 180), ("manufacturer", 180), ("currency", 12),
        ("price_unit", 80), ("retrieved_at", 40),
    ):
        value = candidate.get(key)
        if isinstance(value, str):
            safe_candidate[key] = value[:limit]
    attributes = candidate.get("attributes") if isinstance(candidate.get("attributes"), dict) else {}
    characteristic = attributes.get("characteristic", "")
    if not isinstance(characteristic, str) or len(characteristic) > 300:
        return None
    safe_candidate["history_characteristic"] = characteristic
    price_value = candidate.get("price")
    if price_value is None or isinstance(price_value, (str, int, float)) and len(str(price_value)) <= 100:
        safe_candidate["price"] = price_value
    safe_candidate["retrieval_classification"] = "FUZZY"
    safe_provenance = _safe_price_provenance(candidate)
    safe_match = _safe_history_review_match(candidate_match)
    if not _has_compact_fuzzy_history_provenance(candidate, safe_candidate, safe_provenance):
        return None
    reason = fuzzy_confirmation_eligibility_reason(
        source, safe_candidate, safe_provenance, safe_match, route,
        expected_snapshot_version=route.get("history_catalog_version"),
        physical_excel_row=source.get("excel_row"), retrieval_rank=retrieval_rank,
    )
    if reason is not None:
        return None
    safe_provenance = {
        key: value for key, value in safe_provenance.items()
        if key not in {"normalizer_revision", "normalized_name_signature"}
    }
    raw_provenance = candidate.get("data_provenance")
    raw_provenance = raw_provenance if isinstance(raw_provenance, dict) else {}
    counterparty, counterparty_cut = bounded_utf8_text(
        raw_provenance.get("counterparty"), MAX_COUNTERPARTY_DISPLAY_BYTES,
    )
    counterparty_truncated = counterparty_cut or raw_provenance.get("counterparty_truncated") is True
    safe_match = {
        key: safe_match.get(key, [])
        for key in ("decision", "offer_id", "conflicting_attributes", "missing_attributes")
    }
    compact_v1 = {
        "classification": "FUZZY",
        "provider": "1c",
        "identity": [
            safe_candidate.get("offer_id"), safe_candidate.get("source_item_id"),
            safe_candidate.get("title"), safe_candidate.get("article", ""),
            safe_candidate.get("manufacturer", ""), safe_candidate.get("history_characteristic", ""),
            safe_candidate.get("price"), safe_candidate.get("currency"),
            safe_candidate.get("price_unit"), safe_candidate.get("retrieved_at"),
        ],
        "provenance": [
            "1c", "purchase", safe_provenance.get("snapshot_version"),
            safe_provenance.get("history_item_id"), safe_provenance.get("selected_event_id"),
            safe_provenance.get("purchase_date"), "gross",
            safe_provenance.get("effective_unit_price_gross"),
            "s" if safe_provenance.get("currency_basis") == "source" else "d",
            safe_provenance.get("unit_family"),
        ],
        "match": [
            safe_match.get("decision"), safe_match.get("offer_id") == safe_candidate.get("offer_id"),
            safe_match.get("conflicting_attributes", []), safe_match.get("missing_attributes", []),
        ],
        "rank": retrieval_rank,
    }
    if "warehouse" in safe_provenance or counterparty or counterparty_truncated:
        return pending_history_candidate_v2(
            compact_v1,
            warehouse=safe_provenance.get("warehouse"),
            counterparty=counterparty,
            warehouse_truncated=safe_provenance.get("warehouse_truncated") is True,
            counterparty_truncated=counterparty_truncated,
        )
    return {"fuzzy_v1": compact_v1}


def canonical_tender_projection(
    result_payload: dict[str, Any],
    source_rows: list[dict[str, Any]],
    selected_ids: list[str],
) -> list[dict[str, Any]]:
    expected = set(selected_ids)
    rows_by_id = {str(row.get("source_row_id") or ""): row for row in source_rows}
    results = result_payload.get("results")
    if not isinstance(results, list):
        raise TenderWorkspaceError("Результаты подбора не прошли проверку соответствия строк.", 409, "TENDER_RESULT_CORRELATION_FAILED")
    identities: list[str] = []
    for result in results:
        intent = result.get("intent") if isinstance(result, dict) else None
        source_row_id = str(intent.get("source_row_id") or "") if isinstance(intent, dict) else ""
        identities.append(source_row_id)
    if len(identities) != len(set(identities)) or set(identities) != expected or len(identities) != len(selected_ids):
        raise TenderWorkspaceError("Результаты подбора не прошли проверку соответствия строк.", 409, "TENDER_RESULT_CORRELATION_FAILED")

    projection: list[dict[str, Any]] = []
    for result, source_row_id in zip(results, identities, strict=True):
        intent = result.get("intent") or {}
        source = rows_by_id[source_row_id]
        offer = result.get("recommended_offer") if isinstance(result.get("recommended_offer"), dict) else None
        matches = result.get("match_results") if isinstance(result.get("match_results"), list) else []
        offers = result.get("offers") if isinstance(result.get("offers"), list) else []
        recommended_match = next(
            (item for item in matches if isinstance(item, dict) and offer and (item.get("offer") or {}).get("offer_id") == offer.get("offer_id")),
            None,
        )
        if recommended_match is None and isinstance(result.get("review_candidate"), dict):
            recommended_match = result["review_candidate"]
        route = result.get("route") if isinstance(result.get("route"), dict) else {}
        history_review_candidates: list[dict[str, Any]] = []
        if route.get("source_mode") == "one_c_only" and route.get("final_source_kind") == "history_review":
            match_by_offer_id = {
                str((item.get("offer") or {}).get("offer_id") or ""): item
                for item in matches
                if isinstance(item, dict) and isinstance(item.get("offer"), dict)
            }
            strong_candidates = []
            fuzzy_candidates = []
            fuzzy_retrieval_rank = 0
            for candidate in offers:
                if not isinstance(candidate, dict):
                    continue
                classification = candidate.get("history_retrieval_classification")
                if classification == "FUZZY":
                    fuzzy_retrieval_rank += 1
                candidate_match = match_by_offer_id.get(str(candidate.get("offer_id") or ""))
                if not isinstance(candidate_match, dict) or candidate_match.get("decision") == "REJECT":
                    continue
                if classification in {"EXACT_ARTICLE", "EXACT_NAME_UNIT", "NORMALIZED_NAME_UNIT"}:
                    strong_candidates.append((candidate, candidate_match))
                elif classification == "FUZZY":
                    fuzzy_candidates.append((candidate, candidate_match, fuzzy_retrieval_rank))

            # Strong evidence remains the only durable confirmation set whenever
            # it exists. Weak candidates are retained only for fuzzy-only review.
            selected_candidates = [
                (candidate, candidate_match, None)
                for candidate, candidate_match in strong_candidates[:_MAX_HISTORY_REVIEW_CANDIDATES_PER_ROW]
            ]
            for candidate, candidate_match, retrieval_rank in selected_candidates:
                classification = candidate.get("history_retrieval_classification")
                safe_candidate: dict[str, Any] = {}
                identity_fields = (
                    ("offer_id", 180), ("provider", 40), ("source_item_id", 180), ("title", 500),
                    ("article", 180), ("manufacturer", 180), ("currency", 12),
                    ("price_unit", 80), ("retrieved_at", 40),
                )
                identity_fields = identity_fields[:6] + (("brand", 180),) + identity_fields[6:]
                for key, limit in identity_fields:
                    value = candidate.get(key)
                    if isinstance(value, str):
                        safe_candidate[key] = value[:limit]
                if candidate.get("provider") == "one_c_history":
                    attributes = candidate.get("attributes") if isinstance(candidate.get("attributes"), dict) else {}
                    characteristic = attributes.get("characteristic", "")
                    if not isinstance(characteristic, str) or len(characteristic) > 300:
                        continue
                    safe_candidate["history_characteristic"] = characteristic
                price_value = candidate.get("price")
                if price_value is None or isinstance(price_value, (str, int, float)) and len(str(price_value)) <= 100:
                    safe_candidate["price"] = price_value
                safe_candidate["retrieval_classification"] = classification
                safe_provenance = _safe_price_provenance(candidate)
                safe_match = _safe_history_review_match(candidate_match)
                durable_candidate = {
                    "offer": safe_candidate,
                    "price_provenance": safe_provenance,
                    "match": safe_match,
                }
                history_review_candidates.append(durable_candidate)

            if not selected_candidates:
                # Allocate the three durable fuzzy slots by eligibility while
                # retaining each candidate's original retrieval rank.
                for candidate, candidate_match, retrieval_rank in fuzzy_candidates:
                    compact = _compact_fuzzy_projection_candidate(
                        candidate, candidate_match, source, route, retrieval_rank,
                    )
                    if compact is None:
                        continue
                    history_review_candidates.append(compact)
                    if len(history_review_candidates) == _MAX_HISTORY_REVIEW_CANDIDATES_PER_ROW:
                        break
        notices = result.get("notices") if isinstance(result.get("notices"), list) else []
        notice_codes = sorted({
            str(item.get("code")) for item in notices
            if isinstance(item, dict) and _SAFE_CODE_RE.fullmatch(str(item.get("code") or ""))
        })
        safe_offer = None
        if offer:
            safe_offer = {
                key: offer.get(key)
                for key in (
                    "offer_id", "provider", "source_item_id", "title", "article", "manufacturer", "brand",
                    "price", "currency", "price_unit", "retrieved_at", "availability", "availability_text",
                )
                if key in offer
            }
        route_projection = {
            key: route.get(key)
            for key in (
                "source_mode", "final_source_kind", "fallback_status", "history_outcome",
                "history_safe_basis", "history_reason_code", "history_catalog_version",
                "history_selected_event_id", "history_purchase_date", "history_age_days",
                "fallback_called", "fallback_provider_key", "fallback_catalog_version",
                "routing_policy_revision",
            )
            if key in route
        }
        if history_review_candidates and (
            "fuzzy_v1" in history_review_candidates[0]
            or "_history_candidate_v2_pending" in history_review_candidates[0]
        ):
            route_projection = {
                key: route_projection[key]
                for key in (
                    "source_mode", "final_source_kind", "history_outcome",
                    "history_safe_basis", "history_catalog_version", "fallback_called",
                )
                if key in route_projection
            }
        projection.append({
            "source_row_id": source_row_id,
            "physical_excel_row": int(source.get("excel_row") or 0),
            "status": "completed",
            "decision": (recommended_match or {}).get("decision") or ("OFFER" if offer else "NO_OFFER"),
            "identity": {
                key: intent.get(key)
                for key in ("normalized_name", "product_class", "manufacturer", "brand", "model", "article", "quantity", "unit")
                if key in intent
            },
            "recommended_offer": safe_offer,
            "history_review_candidates": history_review_candidates,
            "price_provenance": _safe_price_provenance(offer),
            "recommended_match": _safe_match(recommended_match),
            "provider_source": {
                "kind": route.get("final_source_kind"),
                "provider": (safe_offer or {}).get("provider") or route.get("fallback_provider_key") or "",
                "source_item_id": (safe_offer or {}).get("source_item_id") or route.get("history_selected_event_id") or "",
            },
            "route": route_projection,
            "warning_codes": notice_codes,
        })
    return projection


__all__ = ["TenderSourcingRowAdapter", "TenderSourcingRunStore", "canonical_tender_projection"]
