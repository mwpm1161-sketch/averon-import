"""Shared safety policy for critical OCR fields and review state."""

from __future__ import annotations

from collections.abc import Iterable

from averon_import.core.normalizers import numeric_cell_metadata

CRITICAL_FIELDS = ("quantity", "unit", "mass")
CRITICAL_REASONS = {
    "critical_value_missing",
    "numeric_suspect",
    "ambiguous_columns",
    "ambiguous_table_schema",
    "secondary_conflict",
    "structural_ambiguity",
    "structural_disagreement",
    "structural_boundary_conflict",
    "word_assignment_ambiguity",
    "physical_row_unresolved",
    "unsupported_table_schema",
    "schema_unknown",
    "structural_layout_ambiguous",
    "structural_schema_ambiguous",
    "physical_row_loss_suspected",
    "identity_cell_missing",
}

# These reasons describe unresolved evidence, rather than a legacy row-role
# guess.  Semantic projection may carry them from the physical row into the
# logical item, while deliberately dropping ``no_confidence`` and
# ``context_missing`` from the legacy assembler.
SEMANTIC_SOURCE_SAFETY_REASONS = frozenset(CRITICAL_REASONS - {
    "critical_value_missing",
})

STRUCTURAL_REVIEW_REASONS = frozenset({
    "structural_ambiguity",
    "structural_disagreement",
    "structural_boundary_conflict",
    "word_assignment_ambiguity",
})


def _semantic_structural_impact(row: object) -> str:
    metadata = _row_metadata(row)
    if isinstance(row, dict):
        value = row.get("semantic_structural_impact")
    else:
        value = None
    value = value or metadata.get("semantic_structural_impact")
    return str(value or "").strip().upper()


def _values(row: dict) -> dict:
    return row if isinstance(row, dict) else {}


def _row_metadata(row: object) -> dict:
    if isinstance(row, dict):
        metadata = row.get("ocr_metadata") or {}
        return metadata if isinstance(metadata, dict) else {}
    metadata = getattr(row, "metadata", {}) or {}
    return metadata if isinstance(metadata, dict) else {}


def _row_values(row: object) -> dict:
    if isinstance(row, dict):
        return row
    values = getattr(row, "values", {}) or {}
    return values if isinstance(values, dict) else {}


def is_semantic_authoritative_row(row: object) -> bool:
    metadata = _row_metadata(row)
    values = _row_values(row)
    return bool(
        values.get("semantic_authoritative")
        or metadata.get("semantic_authoritative")
    )


def required_critical_fields(row: object) -> tuple[str, ...]:
    """Return the applicable critical fields for one row.

    The legacy path intentionally retains the historical three-field rule.
    Semantic-authoritative rows use the explicit projection contract; a
    missing contract fails closed to the historical set rather than silently
    allowing an unvalidated row through.
    """

    if not is_semantic_authoritative_row(row):
        return CRITICAL_FIELDS
    metadata = _row_metadata(row)
    declared = metadata.get("semantic_required_critical_fields")
    if isinstance(declared, (list, tuple, set)):
        return tuple(
            field for field in CRITICAL_FIELDS
            if field in {str(item) for item in declared}
        )
    return CRITICAL_FIELDS


def semantic_missing_critical_fields(row: object) -> list[str]:
    """Apply the semantic applicability contract to an OCR or UI row."""

    metadata = _row_metadata(row)
    values = _row_values(row)
    role = str(
        values.get("row_type")
        or metadata.get("semantic_row_type")
        or ("item" if metadata.get("semantic_role") == "ITEM_ROOT" else "")
    )
    if not is_semantic_authoritative_row(row) or role not in {
        "item", "component", "item_candidate"
    }:
        return []
    return [
        field for field in required_critical_fields(row)
        if not str(values.get(field, "") or "").strip()
    ]


def mark_semantic_field_required(row: object, field: str) -> None:
    """Record positive evidence that a semantic critical field is applicable."""

    if field not in CRITICAL_FIELDS or not is_semantic_authoritative_row(row):
        return
    metadata = _row_metadata(row)
    declared = metadata.get("semantic_required_critical_fields")
    fields = [
        item for item in (declared if isinstance(declared, (list, tuple, set)) else ())
        if str(item) in CRITICAL_FIELDS
    ]
    if field not in fields:
        fields.append(field)
    metadata["semantic_required_critical_fields"] = [
        item for item in CRITICAL_FIELDS if item in fields
    ]


def is_critical_row(row: dict) -> bool:
    row = _values(row)
    metadata = row.get("ocr_metadata") or {}
    if metadata.get("provider") != "yandex_vision":
        return False
    structural_reasons = {
        "structural_ambiguity",
        "structural_disagreement",
        "structural_boundary_conflict",
        "word_assignment_ambiguity",
        "identity_cell_missing",
    }
    if _semantic_structural_impact(row) == "INFORMATIONAL":
        structural_reasons.difference_update(STRUCTURAL_REVIEW_REASONS)
    row_reasons = set(_as_list(row.get("review_reasons")))
    if (
        row.get("structured_table")
        and row_reasons.intersection(structural_reasons)
    ):
        return any(str(row.get(key, "") or "").strip() for key in (
            "name", "position", "type_mark", "code", "manufacturer", "note",
        ))
    if row.get("row_type") not in {"item", "component", "item_candidate"}:
        return False
    product_evidence = any(str(row.get(key, "") or "").strip() for key in (
        "name", "position", "type_mark", "code", "manufacturer",
    ))
    if product_evidence:
        return True
    return any(str(row.get(key, "") or "").strip() for key in ("unit", "quantity", "mass"))


def is_critical_values(values: dict) -> bool:
    """Recognize an item-like OCR row before the final row type is known."""
    values = values if isinstance(values, dict) else {}
    if not str(values.get("name", "") or "").strip():
        return False
    return bool(
        any(str(values.get(key, "") or "").strip() for key in CRITICAL_FIELDS)
        or any(str(values.get(key, "") or "").strip() for key in (
            "position", "type_mark", "code", "manufacturer",
        ))
    )


def missing_critical_fields(row: dict) -> list[str]:
    if is_semantic_authoritative_row(row):
        return semantic_missing_critical_fields(row)
    if not is_critical_row(row):
        return []
    return [
        key for key in CRITICAL_FIELDS
        if not str(row.get(key, "") or "").strip()
    ]


def _as_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item) for item in value if str(item).strip()]


def _numeric_suspect_fields(row: dict) -> set[str]:
    metadata = row.get("ocr_metadata") or {}
    normalization = metadata.get("normalization") if isinstance(metadata, dict) else {}
    result: set[str] = set()
    if isinstance(normalization, dict):
        for key in ("quantity", "mass"):
            details = normalization.get(key)
            if isinstance(details, dict) and details.get("numeric_suspect"):
                result.add(key)
    edited_fields = set(_as_list(row.get("edited_fields")))
    for key in result & edited_fields:
        details = numeric_cell_metadata(row.get(key, ""))
        if not details.get("numeric_suspect"):
            result.discard(key)
    return result


def critical_blockers_for_row(row: dict) -> list[str]:
    """Return blocking reason codes; ``no_confidence`` is informational only."""
    missing = missing_critical_fields(row)
    reasons = set(_as_list(row.get("review_reasons")))
    reasons.update(_as_list(row.get("critical_blockers")))
    blockers: list[str] = []
    if missing or "critical_value_missing" in reasons:
        blockers.append("critical_value_missing")
    for reason in (
        "numeric_suspect",
        "ambiguous_columns",
        "ambiguous_table_schema",
        "secondary_conflict",
        "structural_ambiguity",
        "structural_disagreement",
        "structural_boundary_conflict",
        "word_assignment_ambiguity",
        "physical_row_unresolved",
        "unsupported_table_schema",
        "schema_unknown",
        "structural_layout_ambiguous",
        "structural_schema_ambiguous",
        "physical_row_loss_suspected",
        "identity_cell_missing",
    ):
        if (
            reason in STRUCTURAL_REVIEW_REASONS
            and _semantic_structural_impact(row) == "INFORMATIONAL"
        ):
            continue
        if reason in reasons:
            blockers.append(reason)
    return blockers


def critical_field_count(row: dict) -> int:
    missing = set(missing_critical_fields(row))
    count = len(missing)
    suspect = _numeric_suspect_fields(row) - missing
    blockers = critical_blockers_for_row(row)
    count += len(suspect)
    if "numeric_suspect" in blockers and not suspect:
        count = max(count, 1)
    if "critical_value_missing" in blockers and not count:
        count = max(count, len(_as_list(row.get("critical_fields"))) or 1)
    if not count and any(
        reason in blockers
        for reason in (
            "ambiguous_columns",
            "ambiguous_table_schema",
            "secondary_conflict",
            "structural_ambiguity",
            "structural_disagreement",
            "structural_boundary_conflict",
            "word_assignment_ambiguity",
            "physical_row_unresolved",
            "unsupported_table_schema",
            "schema_unknown",
            "structural_layout_ambiguous",
            "structural_schema_ambiguous",
            "physical_row_loss_suspected",
            "identity_cell_missing",
        )
    ):
        count = 1
    return count


def refresh_review_state(row: dict) -> dict:
    """Recompute critical state after OCR or a manual row edit."""
    if not isinstance(row, dict):
        return row
    reasons = _as_list(row.get("review_reasons"))
    previous_blockers = _as_list(row.get("critical_blockers"))
    reasons.extend(reason for reason in previous_blockers if reason not in reasons)
    edited_fields = set(_as_list(row.get("edited_fields")))
    metadata = row.get("ocr_metadata") or {}
    identity_flagged = bool(
        "identity_cell_missing" in reasons
        or "identity_cell_missing" in previous_blockers
        or (
            isinstance(metadata, dict)
            and metadata.get("identity_cell_missing")
        )
    )
    identity_resolved = bool(
        identity_flagged
        and "position" in edited_fields
        and str(row.get("position", "") or "").strip()
    )
    if identity_resolved:
        reasons = [reason for reason in reasons if reason != "identity_cell_missing"]
        previous_blockers = [
            reason for reason in previous_blockers
            if reason != "identity_cell_missing"
        ]
        if isinstance(metadata, dict):
            metadata["identity_cell_missing"] = False
    elif identity_flagged and "identity_cell_missing" not in reasons:
        reasons.append("identity_cell_missing")
    conflict_fields = set(_as_list(row.get("secondary_conflict_fields")))
    human_verified_fields = set(_as_list(row.get("human_verified_fields")))
    if (
        "secondary_conflict" in edited_fields
        or conflict_fields.intersection(edited_fields)
    ):
        reasons = [reason for reason in reasons if reason != "secondary_conflict"]
    reasons = [
        reason for reason in reasons
        if reason not in {"critical_value_missing", "numeric_suspect"}
    ]
    missing = missing_critical_fields(row)
    if missing:
        reasons.append("critical_value_missing")
    if _numeric_suspect_fields(row):
        reasons.append("numeric_suspect")
    for field, candidate in (row.get("value_candidates") or {}).items():
        if isinstance(candidate, dict) and candidate.get("review_reason"):
            if field in human_verified_fields:
                # The candidate remains immutable OCR evidence, but an
                # explicit human confirmation resolves its review obligation.
                continue
            if (
                field in edited_fields
                and candidate.get("review_reason") == "secondary_conflict"
            ):
                continue
            reasons.append(str(candidate["review_reason"]))
    # Selecting "verified" is an explicit human confirmation. It may clear
    # a suspect/conflict/ambiguous blocker only when all critical fields have
    # a value; an empty critical field can never be confirmed silently.
    if row.get("status") == "verified" and not missing:
        reasons = [
            reason for reason in reasons
            if reason not in {"numeric_suspect", "ambiguous_columns", "secondary_conflict"}
        ]
    unique_reasons = list(dict.fromkeys(reasons))
    row["critical_fields"] = missing
    row["review_reasons"] = unique_reasons
    row["review_reason"] = ", ".join(unique_reasons)
    effective_blockers = set(previous_blockers)
    effective_blockers.difference_update({"critical_value_missing", "numeric_suspect"})
    if (
        "secondary_conflict" not in unique_reasons
        or "secondary_conflict" in edited_fields
        or conflict_fields.intersection(edited_fields)
    ):
        effective_blockers.discard("secondary_conflict")
    if row.get("status") == "verified" and not missing:
        effective_blockers.difference_update({"numeric_suspect", "ambiguous_columns", "secondary_conflict"})
    review_snapshot = dict(row)
    review_snapshot["critical_blockers"] = sorted(effective_blockers)
    row["critical_blockers"] = critical_blockers_for_row(review_snapshot)
    if row["critical_blockers"] and row.get("status") in {"recognized", "verified"}:
        row["status"] = "review"
    return row


def refresh_rows(rows: Iterable[dict]) -> list[dict]:
    return [refresh_review_state(dict(row)) for row in rows]
