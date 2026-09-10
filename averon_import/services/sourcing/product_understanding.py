from __future__ import annotations

import hashlib
import json
import re
import time
from copy import deepcopy
from typing import Any

from pydantic import ValidationError

from averon_import.services.sourcing.models import (
    MatchResult,
    ProductIntent,
    ProductUnderstandingProvenance,
    ProductUnderstandingResult,
    ProductUnderstandingSuggestion,
    SuggestionResolution,
)


PRODUCT_UNDERSTANDING_REVISION = "1"

# Existing matcher/catalog attribute names are the canonical compatibility
# contract. Values use these stable units throughout sourcing.
CANONICAL_ATTRIBUTE_UNITS = {
    "power": "kW",
    "voltage": "V",
    "current": "A",
    "diameter": "mm",
    "pressure": "PN",
    "cable_section": "mm2",
    "dimensions": "mm",
    "cores": "count",
    "protection_class": "code",
    "material": "text",
    "mounting_type": "text",
    "features": "text",
}

_ATTRIBUTE_ALIASES = {
    "power": "power",
    "power_kw": "power",
    "power_w": "power",
    "wattage": "power",
    "rated_power": "power",
    "nominal_power": "power",
    "voltage": "voltage",
    "voltage_v": "voltage",
    "current": "current",
    "current_a": "current",
    "diameter": "diameter",
    "diameter_mm": "diameter",
    "dn": "diameter",
    "pressure": "pressure",
    "pressure_pn": "pressure",
    "pn": "pressure",
    "cores": "cores",
    "core_count": "cores",
    "cable_section": "cable_section",
    "cable_section_mm2": "cable_section",
    "section_mm2": "cable_section",
    "protection_class": "protection_class",
    "ip": "protection_class",
    "dimensions": "dimensions",
    "dimensions_mm": "dimensions",
    "material": "material",
    "mounting_type": "mounting_type",
    "feature": "features",
    "features": "features",
}

_COMMERCIAL_FIELDS = {
    "price", "currency", "availability", "supplier", "url", "offer_id",
    "stock", "delivery", "discount",
}

PRODUCT_UNDERSTANDING_SYSTEM_PROMPT = """Ты — консервативный семантический парсер одной инженерной товарной позиции.
Верни только строгий JSON, совместимый с ProductIntent, без markdown и пояснений.

Правила:
- не переписывай source_row_id, source_text, quantity или unit;
- сохраняй явно указанные manufacturer, model и article в точности;
- не придумывай отсутствующие технические факты;
- evidence может ссылаться только на дословный фрагмент входной строки;
- неподтверждённые интерпретации помещай в preferred_attributes и uncertainties;
- никогда не возвращай price, currency, availability, supplier, URL, offer_id,
  stock, delivery или discount;
- используй канонические ключи: power (кВт), voltage (В), current (А),
  diameter (мм), pressure (PN), cores, cable_section (мм²),
  protection_class, dimensions (мм), material, mounting_type, features;
- не создавай варианты ключей вроде power_w, wattage или rated_power;
- uncertainties и search_queries должны быть короткими списками строк.
"""


def _prompt_fingerprint() -> str:
    return hashlib.sha256(
        PRODUCT_UNDERSTANDING_SYSTEM_PROMPT.encode("utf-8")
    ).hexdigest()[:16]


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


def _scaled_number(value: str, factor: float = 1.0) -> float | int | None:
    parsed = _number(value)
    if parsed is None:
        return None
    scaled = float(parsed) * factor
    return int(scaled) if scaled.is_integer() else scaled


def extract_generic_attributes(text: str) -> dict[str, Any]:
    """Extract conservative, provider-neutral hints without rewriting source text."""

    value = text.replace("×", "x").replace("х", "x")
    attributes: dict[str, Any] = {}
    numeric_patterns = (
        ("diameter", r"(?:\b(?:dn|ду)\s*|[ø⌀]\s*)(\d+(?:[.,]\d+)?)\s*(?:мм|mm)?"),
        ("pressure", r"\b(?:pn|ру)\s*(\d+(?:[.,]\d+)?)"),
        ("voltage", r"(\d+(?:[.,]\d+)?)\s*(?:в|v)(?![a-zа-яё])"),
        ("current", r"(\d+(?:[.,]\d+)?)\s*(?:а|a)(?![a-zа-яё])"),
    )
    for name, pattern in numeric_patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if match:
            attributes[name] = _number(match.group(1))

    power = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(к\s*вт|kw|вт|w)(?![a-zа-яё])",
        value,
        flags=re.IGNORECASE,
    )
    if power:
        unit = re.sub(r"\s+", "", power.group(2).casefold())
        attributes["power"] = _scaled_number(
            power.group(1),
            1.0 if unit in {"квт", "kw"} else 0.001,
        )

    protection = re.search(r"\b(ip\s*\d+[a-zа-я]*)\b", value, flags=re.IGNORECASE)
    if protection:
        attributes["protection_class"] = protection.group(1).upper().replace(" ", "")

    cable = re.search(
        r"(\d+)\s*x\s*(\d+(?:[.,]\d+)?)\s*(мм2|мм²|mm2|mm²)?",
        value,
        flags=re.IGNORECASE,
    )
    cable_context = bool(re.search(r"\b(?:кабел\w*|провод\w*|жил\w*)\b", value, re.IGNORECASE))
    if cable and (cable.group(3) or cable_context):
        attributes["cores"] = int(cable.group(1))
        attributes["cable_section"] = _number(cable.group(2))
    dimensions = re.search(
        r"(\d+(?:[.,]\d+)?)\s*x\s*(\d+(?:[.,]\d+)?)(?:\s*x\s*(\d+(?:[.,]\d+)?))?\s*мм",
        value,
        flags=re.IGNORECASE,
    )
    if dimensions and not cable_context:
        attributes["dimensions"] = "x".join(
            str(_number(part)) for part in dimensions.groups() if part
        )
    return attributes


def normalize_attribute_key(value: object) -> str | None:
    key = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().casefold()).strip("_")
    return _ATTRIBUTE_ALIASES.get(key)


def _canonical_attribute_value(key: str, value: Any, source_key: str) -> Any:
    if isinstance(value, dict) and "value" in value:
        unit = str(value.get("unit") or "").strip()
        value = f"{value['value']} {unit}".strip()
    if key == "protection_class":
        return str(value or "").strip().upper().replace(" ", "")
    if key == "features":
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()][:12]
        return str(value or "").strip()
    if key in {"material", "mounting_type"}:
        return str(value or "").strip()
    if key == "dimensions":
        text = str(value or "").replace("×", "x").replace("х", "x")
        parts = re.findall(r"\d+(?:[.,]\d+)?", text)
        return "x".join(str(_number(part)) for part in parts) if len(parts) in {2, 3} else str(value)
    if key == "power":
        text = str(value or "")
        factor = 0.001 if source_key == "power_w" else 1.0
        if re.search(r"(?:^|\s)(?:вт|w)(?:\s|$)", text, re.IGNORECASE) and not re.search(
            r"(?:к\s*вт|kw)", text, re.IGNORECASE
        ):
            factor = 0.001
        parsed = _scaled_number(text, factor)
        return parsed if parsed is not None else value
    if key in {"voltage", "current", "diameter", "pressure", "cable_section", "cores"}:
        parsed = _number(str(value or ""))
        return parsed if parsed is not None else value
    return value


def _normalize_attribute_mapping(mapping: Any) -> tuple[dict[str, Any], list[tuple[str, Any]]]:
    if mapping in (None, ""):
        return {}, []
    if not isinstance(mapping, dict):
        raise ValueError("AI attributes must be an object")
    normalized: dict[str, Any] = {}
    rejected: list[tuple[str, Any]] = []
    for raw_key, raw_value in mapping.items():
        source_key = str(raw_key or "").strip().casefold()
        if source_key in _COMMERCIAL_FIELDS:
            raise ValueError("commercial fields are forbidden in ProductIntent")
        key = normalize_attribute_key(source_key)
        if key is None:
            rejected.append((str(raw_key), deepcopy(raw_value)))
            continue
        value = _canonical_attribute_value(key, raw_value, source_key)
        if key in normalized and normalized[key] != value:
            raise ValueError("conflicting aliases for one canonical attribute")
        normalized[key] = value
    return normalized, rejected


def _contains_commercial_field(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).strip().casefold() in _COMMERCIAL_FIELDS
            or _contains_commercial_field(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_commercial_field(item) for item in value)
    return False


def _normalize_ai_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[str, Any]]]:
    if _contains_commercial_field(payload):
        raise ValueError("commercial fields are forbidden in ProductIntent")
    normalized = deepcopy(payload)
    rejected: list[tuple[str, Any]] = []
    for field in ("attributes", "required_attributes", "preferred_attributes"):
        mapping, ignored = _normalize_attribute_mapping(normalized.get(field, {}))
        normalized[field] = mapping
        rejected.extend((f"{field}.{key}", value) for key, value in ignored)
    return normalized, rejected


def build_fallback_intent(row: dict[str, Any]) -> ProductIntent:
    name = str(row.get("name") or row.get("title") or "").strip()
    model = str(row.get("type_mark") or row.get("model") or "").strip()
    article = str(row.get("code") or row.get("article") or "").strip()
    manufacturer = str(row.get("manufacturer") or "").strip()
    note = str(row.get("note") or "").strip()
    explicit_source_text = str(row.get("source_text") or "").strip()
    source_text = explicit_source_text or _text(
        row, "name", "title", "type_mark", "model", "code", "article",
        "manufacturer", "unit", "note"
    )
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


def _compact(value: object) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    text = text.replace("×", "x").replace("х", "x")
    return re.sub(r"[^\w]+", "", text, flags=re.UNICODE)


def _evidence_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for child in value.values():
            result.extend(_evidence_strings(child))
        return result
    if isinstance(value, list):
        result: list[str] = []
        for child in value:
            result.extend(_evidence_strings(child))
        return result
    return []


def _grounded(value: object, source_text: str, evidence: dict[str, Any], field: str = "") -> bool:
    needle = _compact(value)
    if not needle:
        return False
    source = _compact(source_text)
    if needle in source:
        return True
    field_evidence = evidence.get(field) if field else None
    # AI-supplied evidence is useful only when the cited span itself is
    # present in the OCR-owned source text; a free-standing claim is not
    # grounding evidence.
    return any(
        needle in _compact(item) and _compact(item) in source
        for item in _evidence_strings(field_evidence)
    )


def _phrase_grounded(value: str, source_text: str) -> bool:
    if _grounded(value, source_text, {}):
        return True
    source_tokens = {token for token in re.findall(r"[\wа-яё]{3,}", source_text.casefold())}
    value_tokens = {token for token in re.findall(r"[\wа-яё]{3,}", value.casefold())}
    return bool(source_tokens & value_tokens)


def _same_attribute(left: Any, right: Any) -> bool:
    left_number = _number(str(left or ""))
    right_number = _number(str(right or ""))
    if left_number is not None and right_number is not None:
        return float(left_number) == float(right_number)
    return _compact(left) == _compact(right)


def _attribute_grounded(
    key: str,
    value: Any,
    fallback: ProductIntent,
    evidence: dict[str, Any],
) -> bool:
    # Numeric/measurement grounding is mechanical: the deterministic parser
    # must independently find the same canonical key/value in source text.
    if key in fallback.attributes:
        return _same_attribute(value, fallback.attributes[key])
    if key in {"material", "mounting_type", "features"}:
        if isinstance(value, list):
            return bool(value) and all(_grounded(item, fallback.source_text, evidence, key) for item in value)
        return _grounded(value, fallback.source_text, evidence, key)
    return False


def merge_intent_with_source(candidate: ProductIntent, fallback: ProductIntent) -> ProductIntent:
    """Merge an AI proposal while keeping OCR-owned facts authoritative."""

    candidate_evidence = deepcopy(candidate.evidence)
    source_owned = {
        "source_row_id": fallback.source_row_id,
        "source_text": fallback.source_text,
        "quantity": fallback.quantity,
        "unit": fallback.unit,
    }
    values = {
        "article": fallback.article,
        "manufacturer": fallback.manufacturer,
        "model": fallback.model,
        "brand": fallback.brand,
    }
    uncertainties = list(dict.fromkeys([*fallback.uncertainties, *candidate.uncertainties]))
    for field in tuple(values):
        proposed = str(getattr(candidate, field) or "").strip()
        if values[field]:
            continue
        if proposed and _grounded(proposed, fallback.source_text, candidate_evidence, field):
            values[field] = proposed
            continue
        if proposed:
            uncertainties.append(f"ai_inferred_{field}_not_grounded")
            values[field] = ""

    attributes = deepcopy(fallback.attributes)
    required = deepcopy(fallback.required_attributes)
    preferred = deepcopy(fallback.preferred_attributes)
    origins = {key: "source_deterministic" for key in fallback.attributes}
    for mapping_name in ("attributes", "required_attributes", "preferred_attributes"):
        mapping = getattr(candidate, mapping_name)
        for key, proposed in mapping.items():
            key = str(key)
            if key in fallback.attributes:
                continue
            if _attribute_grounded(key, proposed, fallback, candidate_evidence):
                attributes.setdefault(key, deepcopy(proposed))
                origins.setdefault(key, "ai_grounded")
                if mapping_name == "required_attributes":
                    required.setdefault(key, deepcopy(proposed))
                elif key not in required:
                    preferred.setdefault(key, deepcopy(proposed))
            else:
                attributes.setdefault(key, deepcopy(proposed))
                preferred.setdefault(key, deepcopy(proposed))
                origins.setdefault(key, "ai_inferred")
                uncertainties.append(f"ai_inferred_attribute:{key}")
    preferred = {key: value for key, value in preferred.items() if key not in required}

    queries = list(fallback.search_queries)
    for query in candidate.search_queries:
        if _phrase_grounded(query, fallback.source_text):
            queries.append(query)
    queries = list(dict.fromkeys(queries))[:4]
    merged_evidence = deepcopy(candidate_evidence)
    merged_evidence["fallback_source"] = deepcopy(fallback.evidence)
    merged_evidence["source_owned_fields"] = source_owned
    merged_evidence["attribute_origins"] = origins
    merged_evidence["mode"] = "ai_safety_merge"
    normalized_name = candidate.normalized_name if _phrase_grounded(candidate.normalized_name, fallback.source_text) else fallback.normalized_name
    return ProductIntent(
        source_row_id=source_owned["source_row_id"],
        source_text=source_owned["source_text"],
        product_class=candidate.product_class or fallback.product_class,
        normalized_name=normalized_name,
        manufacturer=values["manufacturer"],
        brand=values["brand"],
        model=values["model"],
        article=values["article"],
        attributes=attributes,
        required_attributes=required,
        preferred_attributes=preferred,
        quantity=source_owned["quantity"],
        unit=source_owned["unit"],
        search_queries=queries,
        evidence=merged_evidence,
        uncertainties=list(dict.fromkeys(uncertainties)),
    )


def _audit_suggestions(
    baseline: ProductIntent,
    proposal: ProductIntent,
    resolved: ProductIntent,
    rejected_attributes: list[tuple[str, Any]],
) -> list[ProductUnderstandingSuggestion]:
    suggestions: list[ProductUnderstandingSuggestion] = []
    always_locked = {"source_row_id", "source_text", "quantity", "unit"}
    explicitly_locked = {"manufacturer", "model", "article", "brand"}
    for field in (
        "source_row_id", "source_text", "quantity", "unit", "manufacturer",
        "brand", "model", "article", "product_class", "normalized_name",
    ):
        if field not in proposal.model_fields_set:
            continue
        source_value = getattr(baseline, field)
        proposed_value = getattr(proposal, field)
        if proposed_value == source_value:
            continue
        resolved_value = getattr(resolved, field)
        if field in always_locked or (field in explicitly_locked and source_value):
            resolution = SuggestionResolution.SOURCE_LOCKED
            grounded = False
            reason = "explicit_source_value_is_authoritative"
        elif resolved_value != proposed_value:
            resolution = SuggestionResolution.REJECTED_UNGROUNDED
            grounded = False
            reason = "proposal_not_mechanically_grounded_in_source"
        else:
            grounded = _phrase_grounded(str(proposed_value), baseline.source_text)
            resolution = (
                SuggestionResolution.ACCEPTED_GROUNDED
                if grounded
                else SuggestionResolution.PREFERRED_AI_INFERENCE
            )
            reason = "source_span_confirmed" if grounded else "semantic_inference_not_used_as_required_constraint"
        suggestions.append(ProductUnderstandingSuggestion(
            field=field,
            source_value=deepcopy(source_value),
            proposed_value=deepcopy(proposed_value),
            resolution=resolution,
            reason=reason,
            grounded=grounded,
        ))

    proposal_attributes: dict[str, Any] = {}
    for mapping in (proposal.attributes, proposal.required_attributes, proposal.preferred_attributes):
        proposal_attributes.update(mapping)
    origins = resolved.evidence.get("attribute_origins", {})
    for key, proposed_value in proposal_attributes.items():
        source_value = baseline.attributes.get(key)
        if key in baseline.attributes:
            if _same_attribute(source_value, proposed_value):
                continue
            resolution = SuggestionResolution.SOURCE_LOCKED
            grounded = False
            reason = "deterministic_source_attribute_is_authoritative"
        else:
            origin = origins.get(key)
            grounded = origin == "ai_grounded"
            resolution = (
                SuggestionResolution.ACCEPTED_GROUNDED
                if grounded
                else SuggestionResolution.PREFERRED_AI_INFERENCE
            )
            reason = "source_measurement_confirmed" if grounded else "not_a_required_matching_constraint"
        suggestions.append(ProductUnderstandingSuggestion(
            field=f"attributes.{key}",
            source_value=deepcopy(source_value),
            proposed_value=deepcopy(proposed_value),
            resolution=resolution,
            reason=reason,
            grounded=grounded,
        ))

    for path, value in rejected_attributes:
        suggestions.append(ProductUnderstandingSuggestion(
            field=path,
            source_value=None,
            proposed_value=deepcopy(value),
            resolution=SuggestionResolution.REJECTED_UNGROUNDED,
            reason="unsupported_attribute_key",
            grounded=False,
        ))
    return suggestions


class SourcingAIService:
    """Typed sourcing-specific AI adapter; it never writes back to OCR rows."""

    def __init__(self, ai_service: Any | None = None, provider_key: str = "yandex"):
        self.ai_service = ai_service
        self.provider_key = provider_key
        self._last_status = "configured"

    @property
    def model_identity(self) -> str:
        if self.ai_service is None:
            return ""
        try:
            provider = self.ai_service.ensure_provider(self.provider_key)
            return str(getattr(provider, "model", "") or "")
        except (AttributeError, ValueError):
            try:
                return str(
                    self.ai_service.public_config()
                    .get("providers", {})
                    .get(self.provider_key, {})
                    .get("model", "")
                    or ""
                )
            except Exception:
                return ""

    def cache_identity(self) -> dict[str, str]:
        return {
            "mode": "qwen" if self.available else "fallback",
            "provider": self.provider_key,
            "model": self.model_identity,
            "parser_revision": PRODUCT_UNDERSTANDING_REVISION,
            "prompt_fingerprint": _prompt_fingerprint(),
        }

    @property
    def available(self) -> bool:
        if self.ai_service is None:
            return False
        try:
            provider = self.ai_service.ensure_provider(self.provider_key)
        except (AttributeError, ValueError):
            return False
        return bool(getattr(provider, "configured", False))

    def public_config(self) -> dict[str, Any]:
        if self.ai_service is None:
            return {"provider": self.provider_key, "available": False, "status": "not_configured"}
        provider_info = {}
        try:
            provider_info = self.ai_service.public_config().get("providers", {}).get(self.provider_key, {})
        except Exception:
            provider_info = {}
        configured = bool(provider_info.get("configured"))
        return {
            "provider": self.provider_key,
            "label": provider_info.get("label", "Yandex Cloud AI Studio"),
            "model": provider_info.get("model", ""),
            "base_url": provider_info.get("base_url", ""),
            "available": configured,
            "status": self._last_status if configured else "not_configured",
            "parser_revision": PRODUCT_UNDERSTANDING_REVISION,
        }

    def _record_error(self, exc: Exception) -> None:
        text = str(exc).casefold()
        self._last_status = "access_denied" if "401" in text or "403" in text else "unavailable"

    def _safe_warning(self, prefix: str, exc: Exception) -> str:
        self._record_error(exc)
        text = str(exc).casefold()
        if "401" in text or "403" in text:
            return f"{prefix}: ключ не имеет доступа к AI Studio; использован детерминированный fallback"
        if "json" in text:
            return f"{prefix}: Qwen вернул некорректный JSON; использован детерминированный fallback"
        return f"{prefix}: Qwen недоступен; использован детерминированный fallback"

    def _fallback_result(
        self,
        fallback: ProductIntent,
        warnings: list[str],
        *,
        latency_ms: float | None = None,
    ) -> ProductUnderstandingResult:
        return ProductUnderstandingResult(
            baseline_intent=fallback,
            ai_proposal=None,
            resolved_intent=fallback,
            suggestions=[],
            warnings=list(dict.fromkeys(warnings)),
            mode="fallback",
            provenance=ProductUnderstandingProvenance(
                provider=self.provider_key,
                model=self.model_identity,
                parser_revision=PRODUCT_UNDERSTANDING_REVISION,
                latency_ms=latency_ms,
            ),
        )

    def understand_with_audit(
        self,
        row: dict[str, Any],
        fallback: ProductIntent,
    ) -> ProductUnderstandingResult:
        if not self.available:
            return self._fallback_result(
                fallback,
                ["Qwen не настроен; использован детерминированный fallback"],
            )
        started = time.perf_counter()
        try:
            provider = self.ai_service.ensure_provider(self.provider_key)
            prompt = {
                "source_row_id": fallback.source_row_id,
                "source_text": fallback.source_text,
                "name": row.get("name", ""),
                "type_mark": fallback.model,
                "code": fallback.article,
                "manufacturer": fallback.manufacturer,
                "unit": fallback.unit,
                "quantity": fallback.quantity,
                "note": row.get("note", ""),
            }
            raw = provider.complete([
                {
                    "role": "system",
                    "content": PRODUCT_UNDERSTANDING_SYSTEM_PROMPT,
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
            payload, rejected_attributes = _normalize_ai_payload(payload)
            candidate = ProductIntent.model_validate(payload)
            merged = merge_intent_with_source(candidate, fallback)
            self._last_status = "ready"
            warnings = []
            if rejected_attributes:
                warnings.append("Qwen returned unsupported attribute keys; they were ignored")
            return ProductUnderstandingResult(
                baseline_intent=fallback,
                ai_proposal=candidate,
                resolved_intent=merged,
                suggestions=_audit_suggestions(
                    fallback, candidate, merged, rejected_attributes
                ),
                warnings=warnings,
                mode="qwen",
                provenance=ProductUnderstandingProvenance(
                    provider=self.provider_key,
                    model=str(getattr(provider, "model", "") or self.model_identity),
                    parser_revision=PRODUCT_UNDERSTANDING_REVISION,
                    latency_ms=round((time.perf_counter() - started) * 1000, 3),
                ),
            )
        except (ValueError, ValidationError, TypeError, KeyError) as exc:
            return self._fallback_result(
                fallback,
                [self._safe_warning("AI product understanding failed", exc)],
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
            )
        except Exception as exc:  # provider/network failures are a non-fatal sourcing warning
            return self._fallback_result(
                fallback,
                [self._safe_warning("AI product understanding unavailable", exc)],
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
            )

    def understand(self, row: dict[str, Any], fallback: ProductIntent) -> tuple[ProductIntent, list[str]]:
        """Backward-compatible safe-intent API used by existing callers."""

        result = self.understand_with_audit(row, fallback)
        return result.resolved_intent, result.warnings

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
            evidence = payload.get("evidence", {})
            if not isinstance(evidence, dict):
                evidence = {}
            grouped: dict[str, list[MatchResult]] = {}
            for result in matches:
                if result.decision != "REJECT":
                    grouped.setdefault(result.decision.value, []).append(result)
            ordered = []
            for decision in ("MATCH", "LIKELY_MATCH", "ALTERNATIVE", "REVIEW"):
                group = grouped.get(decision, [])
                group_ids = {result.offer.offer_id for result in group}
                group_order = [offer_id for offer_id in requested if offer_id in group_ids]
                group_order.extend(result.offer.offer_id for result in group if result.offer.offer_id not in group_order)
                for offer_id in group_order:
                    result = known[offer_id]
                    reasons = evidence.get(offer_id, [])
                    if not isinstance(reasons, list):
                        reasons = []
                    ordered.append(result.model_copy(update={"ai_evidence": {"reasons": [str(item) for item in reasons[:5]]}}))
            ordered.extend(result for result in matches if result.decision == "REJECT")
            self._last_status = "ready"
            return [result.model_copy(update={"rank": index}) for index, result in enumerate(ordered, 1)], []
        except (ValueError, ValidationError, TypeError, KeyError) as exc:
            return matches, [self._safe_warning("AI sourcing ranking failed", exc)]
        except Exception as exc:
            return matches, [self._safe_warning("AI sourcing ranking unavailable", exc)]


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
