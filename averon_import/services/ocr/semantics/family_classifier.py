"""Provider-neutral table-family evidence.

The classifier is deliberately independent from semantic column mapping.  It
does not import the mapper, inspect mapper scores, or decide canonical field
assignments.  Its only input is bounded raw context around one table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from averon_import.services.ocr.semantics.context_evidence import BoundedFamilyContext


SUPPORTED_SPECIFICATION = "SUPPORTED_SPECIFICATION"
OTHER_TABLE = "OTHER_TABLE"
AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True, slots=True)
class FamilyEvidence:
    code: str
    kind: str
    region_kind: str
    text: str
    strength: str
    provenance: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "kind": self.kind,
            "region_kind": self.region_kind,
            "text": self.text,
            "strength": self.strength,
            "provenance": [dict(item) for item in self.provenance],
        }


@dataclass(frozen=True, slots=True)
class TableFamilyAssessment:
    family: str
    status: str
    score: float
    evidence_strength: str
    positive_evidence: tuple[FamilyEvidence, ...] = ()
    negative_evidence: tuple[FamilyEvidence, ...] = ()
    weak_evidence: tuple[FamilyEvidence, ...] = ()
    context_regions_used: tuple[dict[str, Any], ...] = ()
    reasons: tuple[str, ...] = ()
    alternatives: tuple[str, ...] = ()
    provenance: tuple[dict[str, Any], ...] = ()

    @property
    def confirmed_other(self) -> bool:
        return self.family == OTHER_TABLE

    def as_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "status": self.status,
            "score": round(float(self.score), 4),
            "evidence_strength": self.evidence_strength,
            "positive_evidence": [item.as_dict() for item in self.positive_evidence],
            "negative_evidence": [item.as_dict() for item in self.negative_evidence],
            "weak_evidence": [item.as_dict() for item in self.weak_evidence],
            "context_regions_used": [dict(item) for item in self.context_regions_used],
            "reasons": list(self.reasons),
            "alternatives": list(self.alternatives),
            "provenance": [dict(item) for item in self.provenance],
        }


class TableFamilyClassifier:
    """Classify only bounded document/table-family language."""

    _POSITIVE = (
        ("specification_family", ("спецификац", "ведомость материалов", "перечень материалов", "техническая спецификац")),
    )
    # A generic header/body word is not a document-family decision.  Keep
    # explicit captions strong and require bounded combinations for profile
    # families.  The classifier remains deliberately independent from the
    # semantic mapper.
    _EXPLICIT_NEGATIVE = (
        ("cable_journal", ("кабельный журнал",)),
        ("route_register", ("журнал трасс", "ведомость трасс", "маршрутная ведомость")),
        ("element_schedule", ("ведомость элементов",)),
        ("metal_schedule", ("ведомость металла", "марка металла")),
        ("cost_document", ("сметная документация", "локальная смета", "ведомость стоимости")),
        ("test_protocol", ("протокол испытан",)),
    )

    _WEAK_NEGATIVE = (
        ("cable_journal", ("кабельн", "кабел")),
        ("route_register", ("трасс", "начало", "конец")),
        ("metal_schedule", ("сечени", "усили", "усил", "профил", "металл")),
        ("cost_document", ("смет", "расцен", "стоимост", "цен", "сумм")),
    )

    def assess(self, context: BoundedFamilyContext) -> TableFamilyAssessment:
        positive: list[FamilyEvidence] = []
        negative: list[FamilyEvidence] = []
        weak: list[FamilyEvidence] = []
        usable_regions = [
            region
            for region in context.regions
            if region.kind in {"table_caption", "near_table_above", "compatibility_context", "header"}
            and str(region.text or "").strip()
        ]
        bounded_text = " ".join(
            " ".join(str(region.text).lower().replace("ё", "е").split())
            for region in usable_regions
        )
        for region in context.regions:
            normalized = " ".join(str(region.text).lower().replace("ё", "е").split())
            if not normalized or region.kind not in {"table_caption", "near_table_above", "compatibility_context", "header"}:
                continue
            for code, phrases in self._POSITIVE:
                if any(phrase in normalized for phrase in phrases):
                    positive.append(FamilyEvidence(code, "positive", region.kind, region.text, "strong", region.provenance))
            for code, phrases in self._EXPLICIT_NEGATIVE:
                if any(phrase in normalized for phrase in phrases):
                    negative.append(FamilyEvidence(code, "negative", region.kind, region.text, "strong", region.provenance))
            for code, phrases in self._WEAK_NEGATIVE:
                if any(phrase in normalized for phrase in phrases):
                    weak.append(FamilyEvidence(code, "negative", region.kind, region.text, "weak", region.provenance))

        # Profile evidence must contain compatible concepts in the same
        # bounded context.  One occurrence of ``сечение``/``стоимость``/
        # ``кабель`` is intentionally retained only as weak diagnostics.
        profile_hits = {
            "cost_price": "цена" in bounded_text or "цен" in bounded_text,
            "cost_value": any(token in bounded_text for token in ("стоимост", "сумм", "расцен")),
            "route": any(token in bounded_text for token in ("трасс", "маршрут")),
            "route_endpoint": any(token in bounded_text for token in ("начало", "конец")),
            "route_cable": "кабел" in bounded_text,
            "metal_mark": "марка металла" in bounded_text,
            "metal_section": "сечени" in bounded_text,
            "metal_strength": "усили" in bounded_text,
            "metal_profile": any(token in bounded_text for token in ("профил", "элемент")),
        }
        profile_codes: set[str] = set()
        if profile_hits["cost_price"] and profile_hits["cost_value"]:
            profile_codes.add("cost_document")
        if (
            profile_hits["route"]
            and (profile_hits["route_endpoint"] or profile_hits["route_cable"])
        ):
            profile_codes.add("route_register")
        if profile_hits["metal_mark"] or sum(
            bool(profile_hits[key]) for key in ("metal_section", "metal_strength", "metal_profile")
        ) >= 2:
            profile_codes.add("metal_schedule")
        for code in sorted(profile_codes):
            matching = next((region for region in usable_regions if code in {
                "cost_document", "route_register", "metal_schedule"
            }), None)
            if matching is None and usable_regions:
                matching = usable_regions[0]
            if matching is not None:
                negative.append(FamilyEvidence(
                    code, "negative", matching.kind, matching.text, "strong", matching.provenance
                ))
        positive = list({(item.code, item.region_kind, item.text): item for item in positive}.values())
        negative = list({(item.code, item.region_kind, item.text): item for item in negative}.values())
        weak = list({(item.code, item.region_kind, item.text): item for item in weak}.values())
        reasons: list[str] = []
        if positive and negative:
            reasons.append("conflicting_family_evidence")
            family = AMBIGUOUS
            status = AMBIGUOUS
            strength = "conflicting"
            score = 0.0
        elif negative:
            family = OTHER_TABLE
            status = OTHER_TABLE
            strength = "strong"
            score = 1.0
            reasons.append("confirmed_other_table")
        elif positive:
            family = SUPPORTED_SPECIFICATION
            status = SUPPORTED_SPECIFICATION
            strength = "strong"
            score = 1.0
            reasons.append("positive_document_family:" + ",".join(sorted({item.code for item in positive})))
        else:
            family = AMBIGUOUS
            status = AMBIGUOUS
            strength = "missing"
            score = 0.0
            reasons.append("family_context_missing")
        return TableFamilyAssessment(
            family=family,
            status=status,
            score=score,
            evidence_strength=strength,
            positive_evidence=tuple(positive),
            negative_evidence=tuple(negative),
            weak_evidence=tuple(weak),
            context_regions_used=tuple(region.as_dict() for region in context.regions),
            reasons=tuple(reasons),
            alternatives=(OTHER_TABLE, SUPPORTED_SPECIFICATION) if family == AMBIGUOUS else (),
            provenance=context.provenance,
        )

    classify = assess


DEFAULT_TABLE_FAMILY_CLASSIFIER = TableFamilyClassifier()
