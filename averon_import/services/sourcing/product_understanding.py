from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import ValidationError

from averon_import.services.sourcing.models import MatchResult, ProductIntent


def _text(row: dict[str, Any], *keys: str) -> str:
    return " ".join(str(row.get(key) or "").strip() for key in keys if str(row.get(key) or "").strip())


def _source_id(row: dict[str, Any], source_text: str) -> str:
    value = row.get("id") or row.get("source_row_id")
    if value is not None and str(value).strip():
        return str(value)
    page = str(row.get("page") or "")
    source_row = str(row.get("source_row") or "")
    if page or source_row:
        return f"page:{page}:row:{source_row}"
    # Python's built-in hash is intentionally randomized between processes.
    # A sourcing cache key must therefore use a stable digest.
    digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()[:24]
    return "row:" + digest


def _number(value: str) -> float | int | None:
    match = re.search(r"\d+(?:[.,]\d+)?", value)
    if not match:
        return None
    parsed = float(match.group(0).replace(",", "."))
    return int(parsed) if parsed.is_integer() else parsed


def extract_generic_attributes(text: str) -> dict[str, Any]:
    """Extract conservative, provider-neutral hints without rewriting source text."""

    value = text.replace("×", "x").replace("х", "x")
    attributes: dict[str, Any] = {}
    patterns = (
        ("diameter", r"(?:dn|ду)\s*(\d+(?:[.,]\d+)?)\s*(?:мм)?"),
        ("pressure", r"(?:pn|ру)\s*(\d+(?:[.,]\d+)?)"),
        ("voltage", r"(\d+(?:[.,]\d+)?)\s*(?:в|v)\b"),
        ("power", r"(\d+(?:[.,]\d+)?)\s*(?:квт|kw)\b"),
        ("current", r"(\d+(?:[.,]\d+)?)\s*(?:а|a)\b"),
        ("protection_class", r"\b(ip\s*\d+[a-zа-я]*)\b"),
    )
    for name, pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if match:
            raw = match.group(1)
            attributes[name] = _number(raw) if name not in {"protection_class"} else raw.upper().replace(" ", "")
    cable = re.search(
        r"(\d+)\s*x\s*(\d+(?:[.,]\d+)?)\s*(?:мм2|мм²|mm2)?",
        value,
        flags=re.IGNORECASE,
    )
    if cable:
        attributes["cores"] = int(cable.group(1))
        attributes["cable_section"] = _number(cable.group(2))
    dimensions = re.search(
        r"(\d+(?:[.,]\d+)?)\s*x\s*(\d+(?:[.,]\d+)?)(?:\s*x\s*(\d+(?:[.,]\d+)?))?\s*мм",
        value,
        flags=re.IGNORECASE,
    )
    if dimensions:
        attributes["dimensions"] = "x".join(part for part in dimensions.groups() if part)
    return attributes


def build_fallback_intent(row: dict[str, Any]) -> ProductIntent:
    name = str(row.get("name") or row.get("title") or "").strip()
    model = str(row.get("type_mark") or "").strip()
    article = str(row.get("code") or "").strip()
    manufacturer = str(row.get("manufacturer") or "").strip()
    note = str(row.get("note") or "").strip()
    source_text = _text(row, "name", "type_mark", "code", "manufacturer", "unit", "note")
    if not source_text:
        source_text = str(row.get("source_text") or "").strip()
    source_id = _source_id(row, source_text)
    attributes = extract_generic_attributes(source_text)
    required = dict(attributes)
    queries = [value for value in (name, f"{name} {model}".strip(), article) if value]
    product_class = ""
    lowered = source_text.casefold()
    for marker, label in (
        ("кабел", "кабель"),
        ("труба", "трубная продукция"),
        ("клапан", "арматура"),
        ("насос", "насосное оборудование"),
        ("светиль", "светотехника"),
    ):
        if marker in lowered:
            product_class = label
            break
    uncertainties = []
    if not name:
        uncertainties.append("source_name_missing")
    if not str(row.get("quantity") or "").strip():
        uncertainties.append("quantity_unresolved")
    return ProductIntent(
        source_row_id=source_id,
        source_text=source_text,
        product_class=product_class,
        normalized_name=name,
        manufacturer=manufacturer,
        brand=manufacturer,
        model=model,
        article=article,
        attributes=attributes,
        required_attributes=required,
        preferred_attributes={"manufacturer": manufacturer} if manufacturer else {},
        quantity=str(row.get("quantity") or "").strip(),
        unit=str(row.get("unit") or "").strip(),
        search_queries=queries,
        evidence={
            "source_fields": {
                key: str(row.get(key) or "")
                for key in ("name", "type_mark", "code", "manufacturer", "unit", "quantity", "note")
                if str(row.get(key) or "").strip()
            },
            "mode": "deterministic_fallback",
        },
        uncertainties=uncertainties,
    )


class SourcingAIService:
    """Typed sourcing-specific AI adapter; it never writes back to OCR rows."""

    def __init__(self, ai_service: Any | None = None, provider_key: str = "yandex"):
        self.ai_service = ai_service
        self.provider_key = provider_key

    @property
    def available(self) -> bool:
        if self.ai_service is None:
            return False
        try:
            provider = self.ai_service.ensure_provider(self.provider_key)
        except (AttributeError, ValueError):
            return False
        return bool(getattr(provider, "configured", False))

    def understand(self, row: dict[str, Any], fallback: ProductIntent) -> tuple[ProductIntent, list[str]]:
        if not self.available:
            return fallback, ["AI product understanding unavailable; deterministic fallback used"]
        try:
            provider = self.ai_service.ensure_provider(self.provider_key)
            prompt = {
                "source_row_id": fallback.source_row_id,
                "name": row.get("name", ""),
                "type_mark": row.get("type_mark", ""),
                "code": row.get("code", ""),
                "manufacturer": row.get("manufacturer", ""),
                "unit": row.get("unit", ""),
                "quantity": row.get("quantity", ""),
                "note": row.get("note", ""),
            }
            raw = provider.complete([
                {
                    "role": "system",
                    "content": (
                        "Return only strict JSON matching ProductIntent fields. "
                        "Preserve evidence and uncertainties. Never invent commercial facts."
                    ),
                },
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ])
            payload = _extract_json(raw)
            # The OCR-owned source identity is always supplied locally.  It is
            # valid for a provider to omit these transport fields while still
            # returning a complete semantic proposal.
            payload.setdefault("source_row_id", fallback.source_row_id)
            payload.setdefault("source_text", fallback.source_text)
            payload.setdefault("quantity", fallback.quantity)
            payload.setdefault("unit", fallback.unit)
            candidate = ProductIntent.model_validate(payload)
            # Source identity, quantity and unit remain owned by the OCR row.
            candidate = candidate.model_copy(
                update={
                    "source_row_id": fallback.source_row_id,
                    "source_text": fallback.source_text,
                    "quantity": fallback.quantity,
                    "unit": fallback.unit,
                    "evidence": {**candidate.evidence, "fallback_source": fallback.evidence},
                }
            )
            return candidate, []
        except (ValueError, ValidationError, TypeError, KeyError) as exc:
            return fallback, [f"AI product understanding failed; deterministic fallback used: {exc}"]
        except Exception as exc:  # provider/network failures are a non-fatal sourcing warning
            return fallback, [f"AI product understanding unavailable; deterministic fallback used: {exc}"]

    def rank_matches(
        self,
        intent: ProductIntent,
        matches: list[MatchResult],
    ) -> tuple[list[MatchResult], list[str]]:
        """Optionally reorder existing candidates without changing their facts.

        The provider receives a bounded comparison view and may return only
        known offer ids.  It cannot create offers, change decisions, or edit
        provider-owned commercial fields.
        """

        if not self.available or len(matches) < 2:
            return matches, []
        try:
            provider = self.ai_service.ensure_provider(self.provider_key)
            comparison = [
                {
                    "offer_id": result.offer.offer_id,
                    "title": result.offer.title,
                    "article": result.offer.article,
                    "manufacturer": result.offer.manufacturer,
                    "brand": result.offer.brand,
                    "attributes": result.offer.attributes,
                    "decision": result.decision.value,
                    "matched_attributes": result.matched_attributes,
                    "conflicting_attributes": result.conflicting_attributes,
                    "missing_attributes": result.missing_attributes,
                }
                for result in matches
                if result.decision != "REJECT"
            ]
            raw = provider.complete([
                {
                    "role": "system",
                    "content": (
                        "Return only JSON: {\"offer_order\":[known offer ids],"
                        "\"evidence\":{offer_id:[short reasons]}}. "
                        "Rank only known non-rejected offers. Do not create ids "
                        "or change prices, URLs, articles, availability, or decisions."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"intent": intent.model_dump(mode="json"), "offers": comparison},
                        ensure_ascii=False,
                    ),
                },
            ])
            payload = _extract_json(raw)
            known = {result.offer.offer_id: result for result in matches if result.decision != "REJECT"}
            requested = payload.get("offer_order", [])
            if not isinstance(requested, list):
                raise ValueError("offer_order must be a list")
            ordered_ids = [offer_id for offer_id in requested if offer_id in known]
            ordered_ids.extend(offer_id for offer_id in known if offer_id not in ordered_ids)
            evidence = payload.get("evidence", {})
            if not isinstance(evidence, dict):
                evidence = {}
            ordered = []
            for offer_id in ordered_ids:
                result = known[offer_id]
                reasons = evidence.get(offer_id, [])
                if not isinstance(reasons, list):
                    reasons = []
                ordered.append(result.model_copy(update={"ai_evidence": {"reasons": [str(item) for item in reasons[:5]]}}))
            ordered.extend(result for result in matches if result.decision == "REJECT")
            return [result.model_copy(update={"rank": index}) for index, result in enumerate(ordered, 1)], []
        except (ValueError, ValidationError, TypeError, KeyError) as exc:
            return matches, [f"AI sourcing ranking failed; deterministic order used: {exc}"]
        except Exception as exc:
            return matches, [f"AI sourcing ranking unavailable; deterministic order used: {exc}"]


def _extract_json(value: str) -> dict[str, Any]:
    text = str(value or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("AI response did not contain JSON")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("AI response must be an object")
    return data
