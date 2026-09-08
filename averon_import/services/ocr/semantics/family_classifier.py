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
    _NEGATIVE = (
        ("cable_journal", ("кабельный журнал", "кабельн")),
        ("route_register", ("трасс", "начало", "конец")),
        ("element_schedule", ("ведомость элементов",)),
        ("metal_schedule", ("марка металла", "сечени", "усили")),
        ("cost_document", ("смет", "расцен", "стоимост")),
        ("test_protocol", ("протокол испытан",)),
    )

    def assess(self, context: BoundedFamilyContext) -> TableFamilyAssessment:
        positive: list[FamilyEvidence] = []
        negative: list[FamilyEvidence] = []
        for region in context.regions:
            normalized = " ".join(str(region.text).lower().replace("ё", "е").split())
            if not normalized or region.kind not in {"table_caption", "near_table_above", "compatibility_context", "header"}:
                continue
            for code, phrases in self._POSITIVE:
                if any(phrase in normalized for phrase in phrases):
                    positive.append(FamilyEvidence(code, "positive", region.kind, region.text, "strong", region.provenance))
            for code, phrases in self._NEGATIVE:
                if any(phrase in normalized for phrase in phrases):
                    negative.append(FamilyEvidence(code, "negative", region.kind, region.text, "strong", region.provenance))
        positive = list({(item.code, item.region_kind, item.text): item for item in positive}.values())
        negative = list({(item.code, item.region_kind, item.text): item for item in negative}.values())
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
            context_regions_used=tuple(region.as_dict() for region in context.regions),
            reasons=tuple(reasons),
            alternatives=(OTHER_TABLE, SUPPORTED_SPECIFICATION) if family == AMBIGUOUS else (),
            provenance=context.provenance,
        )

    classify = assess


DEFAULT_TABLE_FAMILY_CLASSIFIER = TableFamilyClassifier()
