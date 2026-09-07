"""Safety assessment for tables mapped to Averon's specification schema.

The recognizer deliberately knows nothing about a particular OCR provider or
document.  It accepts a semantic mapping produced from a bounded header
region and decides whether that mapping is safe to use as a specification.
"""

from __future__ import annotations

from dataclasses import dataclass


SUPPORTED = "supported"
AMBIGUOUS = "ambiguous"
UNSUPPORTED = "unsupported"

CORE_FIELDS = frozenset({"name", "unit", "quantity"})
OPTIONAL_FIELDS = frozenset({
    "position",
    "type_mark",
    "code",
    "manufacturer",
    "mass",
    "note",
})


@dataclass(frozen=True, slots=True)
class SchemaAssessment:
    """Result kept in diagnostics and used at the reconstruction boundary."""

    status: str
    coverage_score: float
    uniqueness_score: float
    header_consistency: float
    mapped_fields: tuple[str, ...]
    reasons: tuple[str, ...] = ()

    @property
    def trusted(self) -> bool:
        return self.status == SUPPORTED

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "coverage_score": round(self.coverage_score, 4),
            "uniqueness_score": round(self.uniqueness_score, 4),
            "header_consistency": round(self.header_consistency, 4),
            "mapped_fields": list(self.mapped_fields),
            "reasons": list(self.reasons),
        }


class SupportedSpecificationSchemaRecognizer:
    """Recognize the supported classic specification family.

    The column count is intentionally not part of the schema identity.  A
    table must instead provide all three critical semantic concepts, at least
    one additional specification concept, and enough semantic coverage that
    a large unrelated journal cannot be projected into BASE_COLUMNS.
    """

    def assess(
        self,
        *,
        column_count: int,
        mapping: dict[int, tuple[str, ...]],
        header_rows: set[int],
        header_text: str = "",
        context_text: str = "",
    ) -> SchemaAssessment:
        reasons: list[str] = []
        if column_count <= 0:
            return SchemaAssessment(
                UNSUPPORTED, 0.0, 0.0, 0.0, (), ("invalid_column_count",)
            )
        if not header_rows:
            reasons.append("header_region_missing")

        normalized: dict[int, tuple[str, ...]] = {
            int(column): tuple(str(key) for key in keys)
            for column, keys in mapping.items()
        }
        mapped_fields = {
            key
            for keys in normalized.values()
            for key in keys
            if key in CORE_FIELDS or key in OPTIONAL_FIELDS
        }
        # A combined ``mass + note`` column is a known supported variant of
        # the specification family: body values are separated conservatively
        # by the table mapper.  Combining critical concepts (for example
        # unit + quantity) remains ambiguous and is never trusted.
        multi_field_columns = [
            column
            for column, keys in normalized.items()
            if len(keys) != 1 and set(keys) != {"mass", "note"}
        ]
        inverse: dict[str, list[int]] = {}
        for column, keys in normalized.items():
            for key in keys:
                inverse.setdefault(key, []).append(column)
        duplicate_fields = {
            key for key, columns in inverse.items() if len(set(columns)) > 1
        }
        if multi_field_columns:
            reasons.append("multiple_semantic_fields_in_one_column")
        if duplicate_fields:
            reasons.append("semantic_field_has_multiple_columns")

        coverage = min(
            1.0,
            len(normalized) / max(1, int(column_count)),
        )
        uniqueness = 1.0
        if multi_field_columns or duplicate_fields:
            uniqueness = 0.0
        header_consistency = 1.0 if header_rows else 0.0
        if not CORE_FIELDS.issubset(mapped_fields):
            missing = sorted(CORE_FIELDS - mapped_fields)
            reasons.append("missing_core_fields:" + ",".join(missing))
        if len(mapped_fields & OPTIONAL_FIELDS) < 1:
            reasons.append("insufficient_optional_specification_evidence")
        # A 30-column journal with five familiar words is not a classic
        # specification.  The threshold is deliberately expressed as semantic
        # coverage, not as a magic accepted column count.
        if coverage < 0.45:
            reasons.append("semantic_coverage_too_low")

        family_text = f"{header_text} {context_text}".lower().replace("ё", "е")
        negative_families = (
            "кабельн", "трасс", "начало", "конец", "ведомость элементов",
            "усили", "сечени", "марка металла", "смет", "расцен",
            "стоимост", "протокол испытан",
        )
        # A bare word such as "оборудование" describes a list subject, not
        # the document family.  Only explicit specification-family wording is
        # a positive title/context signal; rich one-to-one headers remain a
        # separate bounded acceptance path below.
        positive_families = (
            "спецификац",
            "ведомость материалов",
            "перечень материалов",
            "техническая спецификац",
        )
        negative_hits = tuple(
            value for value in negative_families if value in family_text
        )
        positive_hits = tuple(
            value for value in positive_families if value in family_text
        )
        if negative_hits:
            reasons.append("negative_document_family:" + ",".join(negative_hits))
        elif positive_hits:
            reasons.append("positive_document_family:" + ",".join(positive_hits))
        else:
            # A name/unit/quantity trio (or a small four-column list) is not
            # enough to establish a classic material/equipment specification.
            # Rich classic headers are accepted without a title because OCR
            # often misses the document heading entirely.
            rich_header = len(mapped_fields) >= 5 and bool(
                mapped_fields.intersection({"type_mark", "code", "manufacturer", "mass", "note"})
            )
            if not rich_header:
                reasons.append("family_evidence_missing")

        blocking_reasons = [
            reason for reason in reasons
            if not reason.startswith("positive_document_family:")
        ]
        if blocking_reasons:
            status = UNSUPPORTED if negative_hits or "semantic_coverage_too_low" in blocking_reasons else (AMBIGUOUS if any(
                reason in {
                    "multiple_semantic_fields_in_one_column",
                    "semantic_field_has_multiple_columns",
                    "header_region_missing",
                    "family_evidence_missing",
                }
                for reason in blocking_reasons
            ) else UNSUPPORTED)
        else:
            status = SUPPORTED
        return SchemaAssessment(
            status=status,
            coverage_score=coverage,
            uniqueness_score=uniqueness,
            header_consistency=header_consistency,
            mapped_fields=tuple(sorted(mapped_fields)),
            reasons=tuple(dict.fromkeys(reasons)),
        )


DEFAULT_SCHEMA_RECOGNIZER = SupportedSpecificationSchemaRecognizer()
