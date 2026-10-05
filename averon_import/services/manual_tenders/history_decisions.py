"""Durable, run-scoped human confirmations for exact 1C history candidates."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import unicodedata
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from averon_import.core.unit_normalization import normalize_unit_family
from averon_import.services.sourcing.history_identity import history_model_characteristic_conflicts

from .parser import parse_unit_basis
from .repository import TenderWorkspaceError, TenderWorkspaceRepository
from .history_fuzzy_eligibility import (
    MAX_FUZZY_RETRIEVAL_RANK,
    fuzzy_confirmation_eligibility_reason,
)

MAX_HISTORY_DECISION_EVENTS_PER_RUN = 1000
MAX_HISTORY_DECISION_LEDGER_BYTES = 2 * 1024 * 1024
_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_HISTORY_CLASSES = {"EXACT_ARTICLE", "EXACT_NAME_UNIT", "NORMALIZED_NAME_UNIT", "FUZZY"}
_OFFER_FIELDS = {
    "offer_id", "provider", "source_item_id", "title", "article", "manufacturer",
    "brand", "price", "currency", "price_unit", "retrieved_at", "retrieval_classification",
    "history_characteristic",
}
_PROVENANCE_FIELDS = {
    "source", "source_kind", "snapshot_version", "history_item_id", "selected_event_id",
    "purchase_date", "price_basis", "effective_unit_price_gross", "currency_basis", "unit_family",
    "normalizer_revision", "normalized_name_signature",
}
_MATCH_FIELDS = {
    "decision", "offer_id", "matched_attributes", "supporting_attributes",
    "conflicting_attributes", "missing_attributes",
}


def _expand_review_candidate(candidate: Any) -> dict[str, Any]:
    """Expand the compact immutable FUZZY_V1 run projection to the review contract."""
    if not isinstance(candidate, dict) or set(candidate) != {"fuzzy_v1"}:
        return candidate if isinstance(candidate, dict) else {}
    compact = candidate.get("fuzzy_v1")
    if (
        not isinstance(compact, dict)
        or set(compact) != {"classification", "provider", "identity", "provenance", "match", "rank"}
        or compact.get("classification") != "FUZZY"
        or compact.get("provider") != "1c"
    ):
        return {}
    identity = compact.get("identity")
    provenance_values = compact.get("provenance")
    match_values = compact.get("match")
    rank = compact.get("rank")
    if (
        not isinstance(identity, list) or len(identity) != 10
        or not isinstance(provenance_values, list) or len(provenance_values) != 10
        or not isinstance(match_values, list) or len(match_values) != 4
        or isinstance(rank, bool) or not isinstance(rank, int)
    ):
        return {}
    (
        offer_id, source_item_id, title, article, manufacturer, characteristic,
        price, currency, price_unit, retrieved_at,
    ) = identity
    source_token, source_kind_token, snapshot, history_item_id, event_id, purchase_date, price_basis_token, effective_price, currency_basis_token, unit_family = provenance_values
    decision, match_offer_id_matches, conflicts, missing = match_values
    if not isinstance(match_offer_id_matches, bool):
        return {}
    currency_basis = "source" if currency_basis_token == "s" else (
        "company_default" if currency_basis_token == "d" else currency_basis_token
    )
    return {
        "offer": {
            "offer_id":offer_id, "provider":"one_c_history", "source_item_id":source_item_id,
            "title":title, "article":article, "manufacturer":manufacturer, "brand":"",
            "history_characteristic":characteristic, "price":price, "currency":currency,
            "price_unit":price_unit, "retrieved_at":retrieved_at,
            "retrieval_classification":"FUZZY",
        },
        "price_provenance": {
            "source":"one_c_history" if source_token == "1c" else source_token,
            "source_kind":"historical_purchase" if source_kind_token == "purchase" else source_kind_token,
            "snapshot_version":snapshot, "history_item_id":history_item_id,
            "selected_event_id":event_id, "purchase_date":purchase_date,
            "price_basis":"gross_including_vat" if price_basis_token == "gross" else price_basis_token,
            "effective_unit_price_gross":effective_price,
            "currency_basis":currency_basis,
            "unit_family":unit_family,
        },
        "match": {
            "decision":decision, "offer_id":offer_id if match_offer_id_matches else "",
            "matched_attributes":[], "supporting_attributes":[],
            "conflicting_attributes":conflicts, "missing_attributes":missing,
        },
        "retrieval_rank":rank,
    }


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or len(str(value)) > 100:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if amount.is_finite() else None


def _text(value: Any, limit: int = 300) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _identity(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е").replace("\u00a0", " ").split())


def _article(value: Any) -> str:
    return _identity(value)


def _safe_match_projection(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) - _MATCH_FIELDS:
        return None
    result: dict[str, Any] = {}
    for key in ("decision", "offer_id"):
        if key in value:
            result[key] = _text(value[key], 180)
    for key in ("matched_attributes", "supporting_attributes", "conflicting_attributes", "missing_attributes"):
        items = value.get(key, [])
        if not isinstance(items, list) or len(items) > 20 or any(not isinstance(item, str) for item in items):
            return None
        result[key] = [_text(item, 80) for item in items]
    return result


def _public_review_candidate(candidate: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    candidate = _expand_review_candidate(candidate)
    if not isinstance(candidate, dict):
        return {}, {}, {}
    raw_offer = candidate.get("offer") if isinstance(candidate.get("offer"), dict) else {}
    offer: dict[str, Any] = {}
    limits = {
        "offer_id":180, "provider":40, "source_item_id":180, "title":500,
        "article":180, "manufacturer":180, "brand":180, "currency":12,
        "price_unit":80, "retrieved_at":40, "retrieval_classification":40,
        "history_characteristic":300,
    }
    for key, limit in limits.items():
        value = raw_offer.get(key)
        if isinstance(value, str):
            offer[key] = value[:limit]
    amount = _as_decimal(raw_offer.get("price"))
    offer["price"] = str(amount) if amount is not None else None
    raw_provenance = candidate.get("price_provenance") if isinstance(candidate.get("price_provenance"), dict) else {}
    provenance = {
        key: value
        for key, value in raw_provenance.items()
        if key in _PROVENANCE_FIELDS and (
            isinstance(value, (str, int, float, bool)) or value is None
        ) and len(str(value)) <= 180
    }
    match = _safe_match_projection(candidate.get("match")) or {}
    return offer, provenance, match


def _candidate_fingerprint(
    *, tender_id: str, workspace: dict[str, Any], run: dict[str, Any], source: dict[str, Any],
    canonical: dict[str, Any], candidate: dict[str, Any],
    confirmation_basis: str | None = None, identity_assertion: str | None = None,
) -> str:
    offer = candidate["offer"]
    provenance = candidate["price_provenance"]
    match = candidate["match"]
    evidence = {
        "tender_id": tender_id,
        "workspace_revision": int(workspace["revision"]),
        "source_sha256": workspace["source_sha256"],
        "run_id": run["run_id"],
        "source_row_id": source["source_row_id"],
        "physical_excel_row": int(canonical["physical_excel_row"]),
        "history_snapshot_version": run.get("history_catalog_version"),
        "retrieval_classification": offer.get("retrieval_classification"),
        "offer_id": offer.get("offer_id"),
        "history_item_id": provenance.get("history_item_id"),
        "selected_event_id": provenance.get("selected_event_id"),
        "purchase_date": provenance.get("purchase_date"),
        "price": offer.get("price"),
        "currency": offer.get("currency"),
        "price_unit": offer.get("price_unit"),
        "price_basis": provenance.get("price_basis"),
        "effective_unit_price_gross": provenance.get("effective_unit_price_gross"),
        "source_identity": {
            key: source.get(key)
            for key in ("name", "resource_code", "article", "manufacturer", "model", "raw_unit", "quantity", "quantity_trusted", "unit_basis")
        },
        "candidate_identity": {
            key: offer.get(key)
            for key in ("title", "article", "manufacturer", "brand", "source_item_id")
        },
        "match_evidence": match,
    }
    # Preserve byte-for-byte fingerprints for existing exact decisions. New
    # normalized decisions bind their explicit authority and normalizer proof.
    if offer.get("retrieval_classification") == "NORMALIZED_NAME_UNIT":
        evidence.update({
            "confirmation_basis": "NORMALIZED_CONFIRMATION",
            "normalizer_revision": provenance.get("normalizer_revision"),
            "normalized_name_signature": provenance.get("normalized_name_signature"),
        })
        history_characteristic = offer.get("history_characteristic")
        if isinstance(history_characteristic, str) and history_characteristic.strip():
            evidence["history_characteristic"] = history_characteristic
    elif offer.get("retrieval_classification") == "FUZZY":
        # Fuzzy decisions use a distinct, explicit authority contract. Preserve
        # legacy exact/normalized fingerprints while binding all weak-identity
        # evidence and the persisted retrieval position here.
        evidence.update({
            "confirmation_basis": confirmation_basis,
            "identity_assertion": identity_assertion,
            "retrieval_rank": candidate.get("retrieval_rank"),
            "history_characteristic": offer.get("history_characteristic", ""),
            "source_identity_full": {
                key: source.get(key)
                for key in ("name", "resource_code", "article", "manufacturer", "model", "raw_unit", "quantity", "quantity_trusted", "unit_basis")
            },
            "candidate_identity_full": {
                key: offer.get(key)
                for key in ("title", "article", "manufacturer", "brand", "source_item_id", "history_characteristic")
            },
            "commercial_provenance": provenance,
            "match_evidence_full": match,
        })
    return _sha256(evidence)


def _confirmation_basis(candidate: dict[str, Any]) -> str:
    offer = candidate.get("offer") if isinstance(candidate.get("offer"), dict) else {}
    classification = offer.get("retrieval_classification")
    if classification == "FUZZY":
        return "FUZZY_MANUAL_CONFIRMATION"
    return "NORMALIZED_CONFIRMATION" if classification == "NORMALIZED_NAME_UNIT" else "EXACT_CONFIRMATION"


def _candidate_gate(
    tender_id: str,
    workspace: dict[str, Any],
    run: dict[str, Any],
    source: dict[str, Any],
    canonical: dict[str, Any],
    candidate: dict[str, Any],
    *, confirmation_mode: str | None = None, explicit_identity_assertion: bool = False,
) -> tuple[str | None, str | None]:
    """Return (reason code, fingerprint); None reason means confirmable."""
    candidate = _expand_review_candidate(candidate)
    if not isinstance(candidate, dict):
        return "HISTORY_CANDIDATE_EVIDENCE_INVALID", None
    offer = candidate.get("offer")
    if not isinstance(offer, dict):
        return "HISTORY_CANDIDATE_EVIDENCE_INVALID", None
    classification = offer.get("retrieval_classification")
    expected_candidate_keys = {"offer", "price_provenance", "match"}
    if classification == "FUZZY":
        expected_candidate_keys.add("retrieval_rank")
        rank = candidate.get("retrieval_rank")
        if isinstance(rank, bool) or not isinstance(rank, int) or not 1 <= rank <= MAX_FUZZY_RETRIEVAL_RANK:
            return "HISTORY_CANDIDATE_EVIDENCE_INVALID", None
    if set(candidate) != expected_candidate_keys:
        return "HISTORY_CANDIDATE_EVIDENCE_INVALID", None
    provenance = candidate.get("price_provenance")
    match = _safe_match_projection(candidate.get("match"))
    if set(offer) - _OFFER_FIELDS:
        return "HISTORY_CANDIDATE_EVIDENCE_INVALID", None
    required_offer_strings = {
        "offer_id":180, "provider":40, "source_item_id":180, "title":500,
        "article":180, "manufacturer":180, "brand":180, "currency":12,
        "price_unit":80, "retrieved_at":40, "retrieval_classification":40,
    }
    if classification == "FUZZY":
        required_offer_strings.pop("brand")
    if any(not isinstance(offer.get(key), str) or len(offer[key]) > limit for key, limit in required_offer_strings.items()):
        return "HISTORY_CANDIDATE_EVIDENCE_INVALID", None
    if "history_characteristic" in offer and (
        not isinstance(offer["history_characteristic"], str) or len(offer["history_characteristic"]) > 300
    ):
        return "HISTORY_CANDIDATE_EVIDENCE_INVALID", None
    if offer.get("price") is not None and (not isinstance(offer.get("price"), (str, int, float)) or isinstance(offer.get("price"), bool) or len(str(offer["price"])) > 100):
        return "HISTORY_CANDIDATE_EVIDENCE_INVALID", None
    if not isinstance(provenance, dict) or set(provenance) - _PROVENANCE_FIELDS or not isinstance(match, dict):
        return "HISTORY_CANDIDATE_EVIDENCE_INVALID", None
    if any(
        value is not None and not isinstance(value, (str, int, float, bool))
        or len(str(value)) > 180
        for value in provenance.values()
    ):
        return "HISTORY_CANDIDATE_EVIDENCE_INVALID", None
    route = canonical.get("route") if isinstance(canonical.get("route"), dict) else {}
    if (
        run.get("status") != "completed"
        or run.get("source_mode") != "one_c_only"
        or route.get("source_mode") != "one_c_only"
        or route.get("final_source_kind") != "history_review"
        or route.get("history_outcome") != "REVIEW"
        or route.get("history_safe_basis") not in (None, "")
    ):
        return "HISTORY_CANDIDATE_ROUTE_INVALID", None
    if (
        not isinstance(run.get("history_catalog_version"), str)
        or not run.get("history_catalog_version")
        or route.get("history_catalog_version") != run.get("history_catalog_version")
    ):
        return "HISTORY_CANDIDATE_SNAPSHOT_INVALID", None
    if classification not in _HISTORY_CLASSES:
        return "HISTORY_CANDIDATE_CLASS_NOT_CONFIRMABLE", None
    if classification == "FUZZY":
        if confirmation_mode != "EXPLICIT_FUZZY_IDENTITY" or explicit_identity_assertion is not True:
            return "HISTORY_CANDIDATE_EXPLICIT_ASSERTION_REQUIRED", None
    elif confirmation_mode is not None or explicit_identity_assertion:
        return "HISTORY_CANDIDATE_CLASS_NOT_CONFIRMABLE", None
    if offer.get("provider") != "one_c_history" or provenance.get("source") != "one_c_history" or provenance.get("source_kind") != "historical_purchase":
        return "HISTORY_CANDIDATE_PROVENANCE_INVALID", None
    if classification == "FUZZY":
        fuzzy_reason = fuzzy_confirmation_eligibility_reason(
            source, offer, provenance, match, route,
            expected_snapshot_version=run.get("history_catalog_version"),
            physical_excel_row=canonical.get("physical_excel_row"),
            retrieval_rank=candidate.get("retrieval_rank"),
        )
        if fuzzy_reason is not None:
            return fuzzy_reason, None
    if history_model_characteristic_conflicts(source.get("model"), offer.get("history_characteristic", "")):
        return "HISTORY_CANDIDATE_SOURCE_CONFLICT", None
    if match.get("offer_id") != offer.get("offer_id") or match.get("decision") == "REJECT":
        return "HISTORY_CANDIDATE_EVIDENCE_INVALID", None
    if match.get("conflicting_attributes"):
        return "HISTORY_CANDIDATE_SOURCE_CONFLICT", None
    offer_id = _text(offer.get("offer_id"), 180)
    item_id = _text(offer.get("source_item_id"), 180)
    if not offer_id or not item_id or provenance.get("history_item_id") != item_id:
        return "HISTORY_CANDIDATE_PROVENANCE_INVALID", None
    snapshot = run.get("history_catalog_version")
    if provenance.get("snapshot_version") != snapshot:
        return "HISTORY_CANDIDATE_SNAPSHOT_INVALID", None
    event_id = _text(provenance.get("selected_event_id"), 180)
    if not event_id or route.get("history_selected_event_id") not in (None, "", event_id):
        return "HISTORY_CANDIDATE_EVENT_INVALID", None
    purchase_date = provenance.get("purchase_date")
    try:
        parsed_date = date.fromisoformat(str(purchase_date))
    except (TypeError, ValueError):
        return "HISTORY_CANDIDATE_DATE_INVALID", None
    if parsed_date > datetime.now(timezone.utc).date() or route.get("history_purchase_date") not in (None, "", purchase_date):
        return "HISTORY_CANDIDATE_DATE_INVALID", None
    price = _as_decimal(offer.get("price"))
    effective = _as_decimal(provenance.get("effective_unit_price_gross"))
    if price is None or effective is None or price <= 0 or price != effective:
        return "HISTORY_CANDIDATE_PRICE_INVALID", None
    if offer.get("currency") != "RUB" or provenance.get("currency_basis") not in {"source", "company_default"}:
        return "HISTORY_CANDIDATE_CURRENCY_INVALID", None
    if provenance.get("price_basis") != "gross_including_vat":
        return "HISTORY_CANDIDATE_PRICE_BASIS_INVALID", None
    source_unit = parse_unit_basis(source.get("raw_unit"))
    offer_unit = parse_unit_basis(offer.get("price_unit"))
    family = normalize_unit_family(offer.get("price_unit"))
    if (
        source_unit.get("trusted") is not True
        or offer_unit.get("trusted") is not True
        or source_unit.get("dimension") != offer_unit.get("dimension")
        or source_unit.get("base_unit") != offer_unit.get("base_unit")
        or not family
        or provenance.get("unit_family") != family
    ):
        return "HISTORY_CANDIDATE_UNIT_CONFLICT", None
    if int(canonical.get("physical_excel_row", -1)) != int(source.get("excel_row", -2)):
        return "HISTORY_CANDIDATE_SOURCE_CONFLICT", None
    source_article = _article(source.get("article"))
    candidate_article = _article(offer.get("article"))
    if source_article and (not candidate_article or source_article != candidate_article):
        return "HISTORY_CANDIDATE_SOURCE_CONFLICT", None
    if classification == "EXACT_ARTICLE" and (not source_article or source_article != candidate_article):
        return "HISTORY_CANDIDATE_SOURCE_CONFLICT", None
    if classification == "EXACT_NAME_UNIT":
        if _identity(source.get("name")) != _identity(offer.get("title")):
            return "HISTORY_CANDIDATE_SOURCE_CONFLICT", None
    elif classification == "NORMALIZED_NAME_UNIT":
        from averon_import.services.sourcing.history_identity import (
            HISTORY_IDENTITY_NORMALIZER_REVISION,
            history_name_signature,
            history_name_signature_digest,
        )

        source_signature = history_name_signature(source.get("name"))
        candidate_signature = history_name_signature(offer.get("title"))
        if (
            _identity(source.get("name")) == _identity(offer.get("title"))
            or not source_signature
            or source_signature != candidate_signature
            or provenance.get("normalizer_revision") != HISTORY_IDENTITY_NORMALIZER_REVISION
            or provenance.get("normalized_name_signature") != history_name_signature_digest(source.get("name"))
        ):
            return "HISTORY_CANDIDATE_SOURCE_CONFLICT", None
    for source_key, offer_key in (("manufacturer", "manufacturer"),):
        source_value = _identity(source.get(source_key))
        candidate_value = _identity(offer.get(offer_key))
        if source_value and candidate_value and source_value != candidate_value:
            return "HISTORY_CANDIDATE_SOURCE_CONFLICT", None
    try:
        fingerprint = _candidate_fingerprint(
            tender_id=tender_id, workspace=workspace, run=run, source=source,
            canonical=canonical, candidate=candidate,
            confirmation_basis="FUZZY_MANUAL_CONFIRMATION" if classification == "FUZZY" else None,
            identity_assertion="SAME_PRODUCT_V1" if classification == "FUZZY" else None,
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return "HISTORY_CANDIDATE_EVIDENCE_INVALID", None
    return None, fingerprint


class TenderHistoryDecisionStore:
    """Append-only owner-scoped decisions, serialized by the workspace lock."""

    def __init__(self, repository: TenderWorkspaceRepository):
        self.repository = repository
        self._lock = threading.RLock()

    def _path(self, workspace_path: Path, tender_id: str, run_id: str) -> Path:
        path = Path(workspace_path).resolve()
        if (
            not _ID_RE.fullmatch(str(tender_id or ""))
            or not _ID_RE.fullmatch(str(run_id or ""))
            or path.parent != self.repository.workspace_root.resolve()
            or path.name != tender_id
        ):
            raise TenderWorkspaceError("Запуск подбора не найден.")
        return path / "history-decisions" / f"{run_id}.json"

    @staticmethod
    def _empty(tender_id: str, run_id: str) -> dict[str, Any]:
        return {"schema_version": 1, "tender_id": tender_id, "run_id": run_id, "decision_revision": 0, "events": []}

    def _read(self, path: Path, tender_id: str, run_id: str) -> dict[str, Any]:
        try:
            if not path.exists():
                return self._empty(tender_id, run_id)
            if path.stat().st_size > MAX_HISTORY_DECISION_LEDGER_BYTES:
                raise ValueError("ledger size")
            value = json.loads(path.read_text(encoding="utf-8"))
            events = value.get("events") if isinstance(value, dict) else None
            if (
                not isinstance(value, dict) or value.get("schema_version") != 1
                or value.get("tender_id") != tender_id or value.get("run_id") != run_id
                or not isinstance(events, list) or len(events) > MAX_HISTORY_DECISION_EVENTS_PER_RUN
                or value.get("decision_revision") != len(events)
            ):
                raise ValueError("ledger shape")
            seen: set[str] = set()
            confirm_ids: set[str] = set()
            revoked_ids: set[str] = set()
            for event in events:
                if not isinstance(event, dict) or event.get("decision_type") not in {"CONFIRM_HISTORY_CANDIDATE", "REVOKE_HISTORY_CONFIRMATION"}:
                    raise ValueError("event shape")
                decision_id = event.get("decision_id")
                if not isinstance(decision_id, str) or not _ID_RE.fullmatch(decision_id) or decision_id in seen:
                    raise ValueError("event id")
                seen.add(decision_id)
                if event.get("tender_id") != tender_id or event.get("run_id") != run_id:
                    raise ValueError("event scope")
                created_at = event.get("created_at")
                try:
                    parsed_created_at = datetime.fromisoformat(created_at)
                except (TypeError, ValueError):
                    raise ValueError("event timestamp")
                if parsed_created_at.tzinfo is None or len(created_at) > 40:
                    raise ValueError("event timestamp")
                actor = event.get("actor")
                if (
                    not isinstance(actor, dict) or set(actor) != {"username", "role"}
                    or not isinstance(actor.get("username"), str)
                    or not actor["username"] or len(actor["username"]) > 128
                    or any(ord(character) < 32 for character in actor["username"])
                    or actor.get("role") not in {"admin", "user"}
                ):
                    raise ValueError("actor shape")
                if event["decision_type"] == "CONFIRM_HISTORY_CANDIDATE":
                    allowed = {
                        "decision_id", "decision_type", "created_at", "actor", "tender_id", "run_id",
                        "source_row_id", "candidate_offer_id", "evidence_fingerprint", "history_snapshot_version",
                        "history_item_id", "selected_event_id",
                    }
                    allowed_with_basis = allowed | {"confirmation_basis"}
                    allowed_fuzzy = allowed | {"confirmation_basis", "identity_assertion"}
                    required = ("source_row_id", "candidate_offer_id", "evidence_fingerprint", "history_snapshot_version", "history_item_id", "selected_event_id")
                    if (
                        set(event) not in (allowed, allowed_with_basis, allowed_fuzzy)
                        or any(not isinstance(event.get(key), str) or not event[key] or len(event[key]) > 200 for key in required)
                        or not _ID_RE.fullmatch(event["source_row_id"])
                        or not re.fullmatch(r"[a-f0-9]{64}", event["evidence_fingerprint"])
                        or event.get("confirmation_basis") not in (None, "NORMALIZED_CONFIRMATION", "FUZZY_MANUAL_CONFIRMATION")
                        or (
                            event.get("confirmation_basis") == "FUZZY_MANUAL_CONFIRMATION"
                            and (set(event) != allowed_fuzzy or event.get("identity_assertion") != "SAME_PRODUCT_V1")
                        )
                        or (
                            event.get("confirmation_basis") != "FUZZY_MANUAL_CONFIRMATION"
                            and ("identity_assertion" in event or set(event) == allowed_fuzzy)
                        )
                    ):
                        raise ValueError("confirmation shape")
                    confirm_ids.add(decision_id)
                else:
                    allowed = {"decision_id", "decision_type", "created_at", "actor", "tender_id", "run_id", "target_decision_id"}
                    target = event.get("target_decision_id")
                    if set(event) != allowed or not isinstance(target, str) or target not in confirm_ids or target in revoked_ids:
                        raise ValueError("revoke shape")
                    revoked_ids.add(target)
            return value
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise TenderWorkspaceError("Подтверждения истории повреждены.", 409, "TENDER_HISTORY_DECISIONS_CORRUPT") from exc

    @staticmethod
    def _atomic_write(path: Path, value: dict[str, Any]) -> None:
        encoded = _canonical_json(value)
        if len(encoded) > MAX_HISTORY_DECISION_LEDGER_BYTES:
            raise TenderWorkspaceError("Журнал подтверждений слишком велик.", 413, "TENDER_HISTORY_DECISIONS_TOO_LARGE")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.stem}.{uuid.uuid4().hex}.", suffix=".tmp", dir=path.parent)
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

    def _validated_context(self, workspace_path: Path, workspace: dict[str, Any], run: dict[str, Any], tender_id: str, run_id: str):
        if run.get("run_id") != run_id or run.get("tender_id") != tender_id or run.get("status") != "completed":
            raise TenderWorkspaceError("Запуск подбора не найден.")
        if run.get("source_sha256") != workspace.get("source_sha256") or run.get("workspace_revision") != workspace.get("revision"):
            raise TenderWorkspaceError("Запуск не соответствует текущей версии тендера.", 409, "TENDER_RUN_WORKSPACE_MISMATCH")
        from .price_export import TenderPriceResolver

        sources, canonicals = TenderPriceResolver.validate_run(workspace, run, tender_id=tender_id, run_id=run_id)
        return sources, canonicals

    def _snapshot_locked(self, workspace_path: Path, workspace: dict[str, Any], run: dict[str, Any], tender_id: str, run_id: str) -> dict[str, Any]:
        sources, canonicals = self._validated_context(workspace_path, workspace, run, tender_id, run_id)
        ledger = self._read(self._path(workspace_path, tender_id, run_id), tender_id, run_id)
        events = ledger["events"]
        revoked = {event["target_decision_id"] for event in events if event["decision_type"] == "REVOKE_HISTORY_CONFIRMATION"}
        valid_confirms: dict[str, list[tuple[int, dict[str, Any], dict[str, Any], str]]] = {}
        candidates_by_row: dict[str, list[dict[str, Any]]] = {}
        for row_id in run["selected_source_row_ids"]:
            source = sources[row_id]
            canonical = canonicals[row_id]
            projected = canonical.get("history_review_candidates")
            candidates = projected if isinstance(projected, list) else []
            candidate_details = []
            for candidate in candidates:
                candidate = _expand_review_candidate(candidate)
                offer, public_provenance, public_match = _public_review_candidate(candidate)
                candidate_id = str((offer or {}).get("offer_id") or "")
                classification = offer.get("retrieval_classification") if isinstance(offer, dict) else None
                if classification == "FUZZY":
                    fuzzy_reason, fuzzy_fingerprint = _candidate_gate(
                        tender_id, workspace, run, source, canonical, candidate,
                        confirmation_mode="EXPLICIT_FUZZY_IDENTITY", explicit_identity_assertion=True,
                    )
                    reason, fingerprint = fuzzy_reason, None
                else:
                    reason, fingerprint = _candidate_gate(tender_id, workspace, run, source, canonical, candidate)
                    fuzzy_reason, fuzzy_fingerprint = "HISTORY_CANDIDATE_CLASS_NOT_CONFIRMABLE", None
                detail = {
                    "candidate_offer_id": candidate_id,
                    "offer": offer,
                    "price_provenance": public_provenance,
                    "match": public_match,
                    "confirmable": classification != "FUZZY" and reason is None,
                    "reason_code": fuzzy_reason if classification == "FUZZY" else reason,
                    "evidence_fingerprint": fingerprint,
                    "decision": None,
                    "confirmation_basis": _confirmation_basis(candidate),
                    "retrieval_rank": candidate.get("retrieval_rank") if classification == "FUZZY" else None,
                    "confirmable_for_explicit_fuzzy": classification == "FUZZY" and fuzzy_reason is None,
                    "fuzzy_evidence_fingerprint": fuzzy_fingerprint,
                }
                candidate_details.append(detail)
                effective_fingerprint = fuzzy_fingerprint if classification == "FUZZY" else fingerprint
                expected_basis = _confirmation_basis(candidate)
                if effective_fingerprint:
                    for index, event in enumerate(events):
                        if (
                            event["decision_type"] == "CONFIRM_HISTORY_CANDIDATE"
                            and event.get("confirmation_basis", "EXACT_CONFIRMATION") == expected_basis
                            and (classification != "FUZZY" or event.get("identity_assertion") == "SAME_PRODUCT_V1")
                            and event.get("source_row_id") == row_id
                            and event.get("candidate_offer_id") == candidate_id
                            and event.get("evidence_fingerprint") == effective_fingerprint
                            and event["decision_id"] not in revoked
                        ):
                            valid_confirms.setdefault(row_id, []).append((index, event, candidate, effective_fingerprint))
            candidates_by_row[row_id] = candidate_details
        effective: dict[str, dict[str, Any]] = {}
        for row_id, confirmations in valid_confirms.items():
            _index, event, candidate, fingerprint = max(confirmations, key=lambda item: item[0])
            item = {
                "decision_id": event["decision_id"],
                "candidate_offer_id": event["candidate_offer_id"],
                "evidence_fingerprint": fingerprint,
                "history_snapshot_version": event["history_snapshot_version"],
                "history_item_id": event["history_item_id"],
                "selected_event_id": event["selected_event_id"],
                "created_at": event["created_at"],
                "candidate": candidate,
                "confirmation_basis": event.get("confirmation_basis", "EXACT_CONFIRMATION"),
            }
            if event.get("confirmation_basis") == "FUZZY_MANUAL_CONFIRMATION":
                item["identity_assertion"] = event["identity_assertion"]
            effective[row_id] = item
            for detail in candidates_by_row.get(row_id, []):
                detail_fingerprint = (
                    detail.get("fuzzy_evidence_fingerprint")
                    if detail.get("confirmation_basis") == "FUZZY_MANUAL_CONFIRMATION"
                    else detail.get("evidence_fingerprint")
                )
                if detail["candidate_offer_id"] == item["candidate_offer_id"] and detail_fingerprint == fingerprint:
                    detail["decision"] = {key: item[key] for key in ("decision_id", "created_at")}
        digest_items = [
            {"source_row_id": row_id, "decision_id": item["decision_id"], "candidate_offer_id": item["candidate_offer_id"], "evidence_fingerprint": item["evidence_fingerprint"]}
            for row_id, item in sorted(effective.items())
        ]
        audit_events = [{key: value for key, value in event.items()} for event in events]
        return {
            "tender_id": tender_id,
            "run_id": run_id,
            "decision_revision": ledger["decision_revision"],
            "decision_digest": _sha256(digest_items),
            "events": audit_events,
            "effective": effective,
            "rows": [
                {"source_row_id": row_id, "candidates": candidates_by_row.get(row_id, []), "effective_decision": effective.get(row_id)}
                for row_id in run["selected_source_row_ids"]
            ],
        }

    def get_snapshot(self, workspace_path: Path, workspace: dict[str, Any], run: dict[str, Any], tender_id: str, run_id: str) -> dict[str, Any]:
        self._path(workspace_path, tender_id, run_id)
        with self.repository._lock, self._lock:
            return self._snapshot_locked(workspace_path, workspace, run, tender_id, run_id)

    def _append(self, path: Path, ledger: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        if len(ledger["events"]) >= MAX_HISTORY_DECISION_EVENTS_PER_RUN:
            raise TenderWorkspaceError("Достигнут предел событий журнала подтверждений.", 409, "TENDER_HISTORY_DECISIONS_LIMIT")
        updated = {**ledger, "decision_revision": ledger["decision_revision"] + 1, "events": [*ledger["events"], event]}
        self._atomic_write(path, updated)
        return updated

    def confirm(
        self, workspace_path: Path, workspace: dict[str, Any], run: dict[str, Any], *, tender_id: str,
        run_id: str, source_row_id: str, candidate_offer_id: str, expected_revision: int,
        actor_username: str, actor_role: str, confirmation_mode: str | None = None,
        explicit_identity_assertion: bool | None = None,
    ) -> dict[str, Any]:
        ledger_path = self._path(workspace_path, tender_id, run_id)
        with self.repository._lock, self._lock:
            snapshot = self._snapshot_locked(workspace_path, workspace, run, tender_id, run_id)
            if snapshot["decision_revision"] != expected_revision:
                raise TenderWorkspaceError("Журнал подтверждений изменился. Обновите список вариантов.", 409, "TENDER_HISTORY_DECISIONS_STALE")
            row_state = next((row for row in snapshot["rows"] if row["source_row_id"] == source_row_id), None)
            if row_state is None:
                raise TenderWorkspaceError("Вариант истории не найден.", 404, "TENDER_HISTORY_CANDIDATE_NOT_FOUND")
            detail = next((item for item in row_state["candidates"] if item["candidate_offer_id"] == candidate_offer_id), None)
            if detail is None:
                raise TenderWorkspaceError("Вариант истории не найден.", 404, "TENDER_HISTORY_CANDIDATE_NOT_FOUND")
            fuzzy_candidate = detail.get("offer", {}).get("retrieval_classification") == "FUZZY"
            if fuzzy_candidate:
                if (
                    confirmation_mode != "EXPLICIT_FUZZY_IDENTITY"
                    or explicit_identity_assertion is not True
                    or detail.get("confirmable_for_explicit_fuzzy") is not True
                    or not detail.get("fuzzy_evidence_fingerprint")
                ):
                    raise TenderWorkspaceError("Для похожей записи требуется отдельное явное подтверждение идентичности.", 409, "TENDER_HISTORY_CANDIDATE_NOT_CONFIRMABLE")
                evidence_fingerprint = detail["fuzzy_evidence_fingerprint"]
            else:
                if confirmation_mode is not None or explicit_identity_assertion is not None or not detail["confirmable"]:
                    raise TenderWorkspaceError("Этот вариант нельзя подтвердить из-за недостаточных или противоречивых данных.", 409, "TENDER_HISTORY_CANDIDATE_NOT_CONFIRMABLE")
                evidence_fingerprint = detail["evidence_fingerprint"]
            if evidence_fingerprint is None:
                raise TenderWorkspaceError("Этот вариант нельзя подтвердить из-за недостаточных или противоречивых данных.", 409, "TENDER_HISTORY_CANDIDATE_NOT_CONFIRMABLE")
            candidate = detail
            offer = candidate["offer"]
            provenance = candidate["price_provenance"]
            event = {
                "decision_id": uuid.uuid4().hex,
                "decision_type": "CONFIRM_HISTORY_CANDIDATE",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "actor": {"username": _text(actor_username, 128), "role": actor_role},
                "tender_id": tender_id,
                "run_id": run_id,
                "source_row_id": source_row_id,
                "candidate_offer_id": candidate_offer_id,
                "evidence_fingerprint": evidence_fingerprint,
                "history_snapshot_version": provenance["snapshot_version"],
                "history_item_id": provenance["history_item_id"],
                "selected_event_id": provenance["selected_event_id"],
            }
            if _confirmation_basis(candidate) == "NORMALIZED_CONFIRMATION":
                event["confirmation_basis"] = "NORMALIZED_CONFIRMATION"
            elif _confirmation_basis(candidate) == "FUZZY_MANUAL_CONFIRMATION":
                event["confirmation_basis"] = "FUZZY_MANUAL_CONFIRMATION"
                event["identity_assertion"] = "SAME_PRODUCT_V1"
            ledger = self._read(ledger_path, tender_id, run_id)
            self._append(ledger_path, ledger, event)
            return self._snapshot_locked(workspace_path, workspace, run, tender_id, run_id)

    def revoke(
        self, workspace_path: Path, workspace: dict[str, Any], run: dict[str, Any], *, tender_id: str,
        run_id: str, decision_id: str, expected_revision: int, actor_username: str, actor_role: str,
    ) -> dict[str, Any]:
        ledger_path = self._path(workspace_path, tender_id, run_id)
        with self.repository._lock, self._lock:
            snapshot = self._snapshot_locked(workspace_path, workspace, run, tender_id, run_id)
            if snapshot["decision_revision"] != expected_revision:
                raise TenderWorkspaceError("Журнал подтверждений изменился. Обновите список вариантов.", 409, "TENDER_HISTORY_DECISIONS_STALE")
            target = next((item for item in snapshot["effective"].values() if item["decision_id"] == decision_id), None)
            if target is None:
                raise TenderWorkspaceError("Подтверждение уже изменилось или отменено.", 409, "TENDER_HISTORY_DECISIONS_STALE")
            event = {
                "decision_id": uuid.uuid4().hex,
                "decision_type": "REVOKE_HISTORY_CONFIRMATION",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "actor": {"username": _text(actor_username, 128), "role": actor_role},
                "tender_id": tender_id,
                "run_id": run_id,
                "target_decision_id": decision_id,
            }
            ledger = self._read(ledger_path, tender_id, run_id)
            self._append(ledger_path, ledger, event)
            return self._snapshot_locked(workspace_path, workspace, run, tender_id, run_id)


__all__ = [
    "MAX_HISTORY_DECISION_EVENTS_PER_RUN",
    "MAX_HISTORY_DECISION_LEDGER_BYTES",
    "TenderHistoryDecisionStore",
]
