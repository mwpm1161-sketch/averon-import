"""Production boundary for the trusted semantic-table pilot.

The semantic analyzers operate on immutable provider-neutral IR.  This module
is the deliberately small adapter at the edge of that IR: it keeps the live
``SemanticTableIR`` object internal, and projects only resolved logical items
and explicitly resolved non-item dispositions into the existing OCR DTO.
Unresolved physical rows are represented as review evidence, never as
guessed canonical values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Any, Mapping

from averon_import.core.normalizers import normalize_cell, numeric_cell_metadata
from averon_import.services.ocr.base import OcrRow
from averon_import.services.ocr.table_ir import (
    BBox,
    PhysicalRowIR,
    PhysicalTableIR,
)
from averon_import.services.ocr.semantics.row_evidence import (
    RowRole,
    RowRoleState,
)
from averon_import.services.ocr.semantics.semantic_table import (
    DispositionValidation,
    LogicalSpecificationItem,
    SemanticTableIR,
)


@dataclass(slots=True)
class StructuredReconstructionResult:
    """Internal reconstruction result shared by provider and page safety.

    ``semantic_table`` is intentionally not serialized into page diagnostics.
    The diagnostics layer uses ``SemanticTableIR.as_dict()``; the live object
    remains available here until projection and safety processing finish.
    """

    rows: list[OcrRow] = field(default_factory=list)
    physical_table: PhysicalTableIR | None = None
    semantic_table: SemanticTableIR | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
    page_size: tuple[float, float] = (0.0, 0.0)
    selected_mode: str = ""
    semantic_candidate: bool = False
    semantic_authoritative: bool = False
    semantic_resolution_error: str | None = None
    physical_ir_constructed: bool = False
    functional_analysis_completed: bool = False
    relation_analysis_completed: bool = False
    semantic_resolution_completed: bool = False


def semantic_table_authoritative_enabled() -> bool:
    """Return the rollout switch for the limited Yandex semantic pilot."""

    raw = os.environ.get("AVERON_SEMANTIC_TABLE_AUTHORITATIVE")
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _mapping(context: Any) -> dict[int, tuple[str, ...]]:
    raw = (getattr(context, "header_mapping", {}) or {}).get("selected_mapping")
    if raw is None:
        raw = (getattr(context, "header_mapping", {}) or {}).get("mapping") or {}
    result: dict[int, tuple[str, ...]] = {}
    if not isinstance(raw, Mapping):
        return result
    for column, fields in raw.items():
        try:
            index = int(column)
        except (TypeError, ValueError):
            continue
        if isinstance(fields, str):
            result[index] = (fields,)
        else:
            result[index] = tuple(str(field) for field in fields)
    return result


def _bbox_fractional(bbox: BBox, page_size: tuple[float, float]) -> dict[str, float]:
    width, height = page_size
    left, top, right, bottom = bbox
    if width <= 0 or height <= 0:
        return {}
    return {
        "x": round(left / width, 6),
        "y": round(top / height, 6),
        "width": round(max(0.0, right - left) / width, 6),
        "height": round(max(0.0, bottom - top) / height, 6),
    }


def _ref_row_index(ref: Any) -> int | None:
    try:
        return int(ref.row_index)
    except (AttributeError, TypeError, ValueError):
        return None


def _physical_cell_records(
    row: PhysicalRowIR,
    mapping: Mapping[int, tuple[str, ...]],
    page_size: tuple[float, float],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for cell in sorted(row.cells, key=lambda item: item.ref.column_index):
        fields = list(mapping.get(cell.ref.column_index, ()))
        records.append({
            "row_index": cell.ref.row.row_index,
            "column_index": cell.ref.column_index,
            "source_cell_index": cell.ref.source_cell_index,
            "fields": fields,
            "raw_text": cell.raw_text,
            "bbox": _bbox_fractional(cell.bbox, page_size),
            "word_refs": [ref.as_dict() for ref in cell.word_refs],
        })
    return records


def _field_candidate_values(field_value: Any) -> list[dict[str, Any]]:
    return [candidate.as_dict() for candidate in field_value.candidates]


def _base_metadata_for_row(
    base_rows: Mapping[int, OcrRow],
    row_index: int,
) -> dict[str, Any]:
    row = base_rows.get(row_index)
    if row is None or not isinstance(row.metadata, dict):
        return {}
    # Copy only reconstruction/provenance metadata.  Values and confidence
    # are always rebuilt from the semantic IR below.
    keep = {
        "provider", "structured_table", "provider_has_explicit_rows",
        "source_table_index", "source_row_index", "source_subrow_index",
        "split_evidence", "structural_ambiguity", "column_mapping",
        "schema_assessment", "normalization", "cell_bboxes",
        "physical_grid_cells", "structural_evidence",
        "informational_structural_disagreement",
        "structural_disagreement", "word_assignment_ambiguity",
        "ambiguous_fields", "weak_critical_assignment",
        "weak_critical_fields", "ambiguous_physical_cells",
    }
    return {
        key: value.copy() if isinstance(value, dict) else list(value) if isinstance(value, list) else value
        for key, value in row.metadata.items()
        if key in keep
    }


def _item_row(
    item: LogicalSpecificationItem,
    table: PhysicalTableIR,
    mapping: Mapping[int, tuple[str, ...]],
    base_rows: Mapping[int, OcrRow],
    provider_key: str,
    page_size: tuple[float, float],
) -> OcrRow:
    root_index = _ref_row_index(item.physical_row_refs[0]) if item.physical_row_refs else 0
    metadata = _base_metadata_for_row(base_rows, root_index or 0)
    values: dict[str, str] = {}
    sources: dict[str, str] = {}
    raw_values: dict[str, str] = {}
    normalization: dict[str, dict[str, Any]] = {}
    cell_bboxes: dict[str, dict[str, float]] = {}
    physical_grid_cells = dict(metadata.get("physical_grid_cells") or {})
    candidates: dict[str, list[dict[str, Any]]] = {}
    review_reasons = list(item.review_reasons)
    source_cell_refs: set[int] = set()
    physical_refs = [ref.as_dict() for ref in item.physical_row_refs]
    raw_cells: list[dict[str, Any]] = []

    rows_by_key = {row.ref.key: row for row in table.rows}
    for ref in item.physical_row_refs:
        row = rows_by_key.get(ref.key)
        if row is not None:
            raw_cells.extend(_physical_cell_records(row, mapping, page_size))

    for field_name, field_value in item.fields.items():
        candidates_for_field = _field_candidate_values(field_value)
        if candidates_for_field:
            candidates[field_name] = candidates_for_field
        for fragment in field_value.source_fragments:
            if fragment.physical_cell_ref is not None:
                source_index = fragment.physical_cell_ref.source_cell_index
                if source_index >= 0:
                    source_cell_refs.add(source_index)
                physical_grid_cells.setdefault(field_name, {
                    "row_index": fragment.physical_cell_ref.row.row_index,
                    "column_index": fragment.physical_cell_ref.column_index,
                    "bbox": _bbox_fractional(fragment.bbox, page_size),
                })
                cell_bboxes.setdefault(
                    field_name,
                    _bbox_fractional(fragment.bbox, page_size),
                )
        if field_value.review_reasons:
            review_reasons.extend(field_value.review_reasons)
        canonical = str(field_value.canonical_text or "").strip()
        if not canonical:
            continue
        normalized = normalize_cell(field_name, canonical)
        if not normalized:
            continue
        values[field_name] = normalized
        raw_values[field_name] = canonical
        sources[field_name] = provider_key
        if field_name in {"quantity", "mass"}:
            normalization[field_name] = numeric_cell_metadata(canonical)
            if normalization[field_name].get("numeric_suspect"):
                review_reasons.append("numeric_suspect")

    if any(
        not str(values.get(field, "") or "").strip()
        for field in ("unit", "quantity", "mass")
    ):
        review_reasons.append("critical_value_missing")
    review_reasons = list(dict.fromkeys(review_reasons))
    metadata.update({
        "provider": provider_key,
        "structured_table": True,
        "provider_has_explicit_rows": True,
        "reconstruction_mode": "geometry_first",
        "semantic_authoritative": True,
        "semantic_resolved": True,
        "semantic_review": bool(review_reasons),
        "semantic_role": RowRole.ITEM_ROOT.value,
        "semantic_state": "REVIEW" if review_reasons else "VERIFIED",
        "logical_item_id": item.logical_id,
        "physical_row_refs": physical_refs,
        "source_row_index": root_index or 0,
        "source_cell_refs": sorted(source_cell_refs),
        "raw_values": raw_values,
        "raw_physical_cells": raw_cells,
        "normalization": normalization,
        "cell_bboxes": cell_bboxes,
        "physical_grid_cells": physical_grid_cells,
        "value_candidates": candidates,
        "review_reasons": review_reasons,
        "semantic_provenance": dict(item.provenance),
        "provides_confidence": False,
    })
    return OcrRow(
        source_row=root_index or 0,
        values=values,
        confidences={},
        sources=sources,
        bbox=_bbox_fractional(item.bbox, page_size),
        metadata=metadata,
    )


def _raw_row_values(
    row: PhysicalRowIR,
    mapping: Mapping[int, tuple[str, ...]],
    page_size: tuple[float, float],
    allowed_fields: set[str],
) -> tuple[dict[str, str], dict[str, str], dict[str, Any], list[dict[str, Any]]]:
    values: dict[str, str] = {}
    sources: dict[str, str] = {}
    bboxes: dict[str, Any] = {}
    records = _physical_cell_records(row, mapping, page_size)
    for cell in row.cells:
        fields = [field for field in mapping.get(cell.ref.column_index, ()) if field in allowed_fields]
        for field_name in fields:
            if field_name in values:
                continue
            value = normalize_cell(field_name, cell.raw_text)
            if not value:
                continue
            values[field_name] = value
            sources[field_name] = "physical_semantic_evidence"
            bboxes[field_name] = _bbox_fractional(cell.bbox, page_size)
    return values, sources, bboxes, records


def _review_row(
    row: PhysicalRowIR,
    mapping: Mapping[int, tuple[str, ...]],
    page_size: tuple[float, float],
    disposition: Any | None,
    provider_key: str,
) -> OcrRow:
    _values, _sources, bboxes, raw_cells = _raw_row_values(
        row, mapping, page_size, set()
    )
    reasons = list(getattr(disposition, "reasons", ()) or ())
    if "physical_row_semantics_unresolved" not in reasons:
        reasons.append("physical_row_semantics_unresolved")
    role = getattr(getattr(disposition, "role", None), "value", "UNKNOWN")
    metadata = {
        "provider": provider_key,
        "structured_table": True,
        "provider_has_explicit_rows": True,
        "reconstruction_mode": "geometry_first",
        "semantic_authoritative": True,
        "semantic_resolved": False,
        "semantic_review": True,
        "semantic_state": "REVIEW",
        "semantic_role": role,
        "source_table_index": row.ref.table.table_index,
        "source_row_index": row.ref.row_index,
        "source_subrow_index": 0,
        "column_mapping": {
            str(column): list(fields) for column, fields in mapping.items()
        },
        "physical_row_refs": [row.ref.as_dict()],
        "source_cell_refs": sorted({
            cell.ref.source_cell_index
            for cell in row.cells
            if cell.ref.source_cell_index >= 0
        }),
        "cell_bboxes": bboxes,
        "raw_physical_cells": raw_cells,
        "raw_values": {},
        "value_candidates": {},
        "review_reasons": list(dict.fromkeys(reasons)),
        "semantic_provenance": dict(getattr(disposition, "provenance", {}) or {}),
        "provides_confidence": False,
    }
    # No raw OCR text is copied into canonical values on an unresolved row.
    # In particular, a numbering-band quantity remains audit evidence only.
    return OcrRow(
        source_row=row.ref.row_index,
        values={},
        confidences={},
        sources={},
        bbox=_bbox_fractional(row.bbox, page_size),
        metadata=metadata,
    )


def _resolved_context_row(
    row: PhysicalRowIR,
    disposition: Any,
    mapping: Mapping[int, tuple[str, ...]],
    table: PhysicalTableIR,
    page_size: tuple[float, float],
    provider_key: str,
) -> OcrRow:
    qualifier = str(getattr(disposition, "qualifier", "") or "")
    role = getattr(disposition, "role", RowRole.NOTE)
    if role == RowRole.CONTEXT and qualifier in {"SECTION", "SYSTEM"}:
        allowed = {"name", "position"}
    elif role == RowRole.COMPONENT:
        allowed = {"name", "type_mark", "code", "manufacturer", "note"}
    else:
        allowed = {"name", "note"}
    values, sources, bboxes, raw_cells = _raw_row_values(
        row, mapping, page_size, allowed
    )
    semantic_role = getattr(role, "value", str(role))
    row_type = {
        "SECTION": "section",
        "SYSTEM": "system",
        "COMPONENT": "component",
    }.get(qualifier if qualifier in {"SECTION", "SYSTEM"} else semantic_role, "note")
    metadata = {
        "provider": provider_key,
        "structured_table": True,
        "provider_has_explicit_rows": True,
        "reconstruction_mode": "geometry_first",
        "semantic_authoritative": True,
        "semantic_resolved": True,
        "semantic_state": "VERIFIED",
        "semantic_role": semantic_role,
        "semantic_qualifier": qualifier or None,
        "source_table_index": table.ref.table_index,
        "source_row_index": row.ref.row_index,
        "source_subrow_index": 0,
        "column_mapping": {
            str(column): list(fields) for column, fields in mapping.items()
        },
        "physical_row_refs": [row.ref.as_dict()],
        "source_cell_refs": sorted({
            cell.ref.source_cell_index
            for cell in row.cells
            if cell.ref.source_cell_index >= 0
        }),
        "cell_bboxes": bboxes,
        "raw_physical_cells": raw_cells,
        "raw_values": dict(values),
        "value_candidates": {},
        "review_reasons": [],
        "provides_confidence": False,
    }
    return OcrRow(
        source_row=row.ref.row_index,
        values=values,
        confidences={},
        sources=sources,
        bbox=_bbox_fractional(row.bbox, page_size),
        metadata=metadata,
    )


def project_semantic_table(
    result: StructuredReconstructionResult,
    *,
    provider_key: str = "yandex_vision",
) -> list[OcrRow]:
    """Project one resolved semantic table without legacy reinterpretation."""

    table = result.physical_table
    semantic = result.semantic_table
    if table is None or semantic is None:
        raise ValueError("semantic projection requires physical and semantic IR")
    mapping = _mapping(semantic.analysis_context)
    base_rows = {
        int(row.metadata.get("source_row_index")): row
        for row in result.rows
        if isinstance(row.metadata, dict)
        and str(row.metadata.get("source_row_index", "")).lstrip("-").isdigit()
    }
    logical_rows: list[tuple[int, OcrRow, set[str]]] = []
    consumed: set[str] = set()
    for item in sorted(
        semantic.logical_items,
        key=lambda value: (_ref_row_index(value.physical_row_refs[0]) if value.physical_row_refs else 0, value.logical_id),
    ):
        projected = _item_row(
            item, table, mapping, base_rows, provider_key, result.page_size
        )
        refs = {ref.key for ref in item.physical_row_refs}
        consumed.update(refs)
        logical_rows.append((projected.source_row, projected, refs))

    disposition_by_key = {
        disposition.physical_row_ref.key: disposition
        for disposition in semantic.dispositions
    }
    context_rows: list[tuple[int, OcrRow, set[str]]] = []
    for row in sorted(table.rows, key=lambda value: value.ref.row_index):
        if not row.nonempty or row.ref.key in consumed:
            continue
        disposition = disposition_by_key.get(row.ref.key)
        if disposition is None:
            context_rows.append((
                row.ref.row_index,
                _review_row(row, mapping, result.page_size, None, provider_key),
                {row.ref.key},
            ))
            continue
        is_confirmed = (
            disposition.validation_state == DispositionValidation.VALIDATED
            and disposition.role_state == RowRoleState.CONFIRMED
        )
        if not is_confirmed or disposition.role in {RowRole.UNKNOWN, RowRole.CONTINUATION}:
            context_rows.append((
                row.ref.row_index,
                _review_row(row, mapping, result.page_size, disposition, provider_key),
                {row.ref.key},
            ))
            continue
        if disposition.role == RowRole.HEADER:
            continue
        context_rows.append((
            row.ref.row_index,
            _resolved_context_row(
                row,
                disposition,
                mapping,
                table,
                result.page_size,
                provider_key,
            ),
            {row.ref.key},
        ))

    output = [row for _index, row, _refs in sorted(
        [*logical_rows, *context_rows],
        key=lambda value: (value[0], value[1].metadata.get("semantic_role", "")),
    )]
    schema_assessment = dict(semantic.analysis_context.schema_assessment or {})
    structural_evidence = dict(semantic.analysis_context.structural_evidence or {})
    grid_cells_by_row: dict[int, dict[str, dict[str, Any]]] = {}
    for physical_row in table.rows:
        fields_for_row: dict[str, dict[str, Any]] = {}
        for cell in physical_row.cells:
            for field_name in mapping.get(cell.ref.column_index, ()):
                fields_for_row.setdefault(field_name, {
                    "row_index": cell.ref.row.row_index,
                    "column_index": cell.ref.column_index,
                    "bbox": _bbox_fractional(cell.bbox, result.page_size),
                })
        grid_cells_by_row[physical_row.ref.row_index] = fields_for_row
    for row in output:
        metadata = row.metadata
        metadata.setdefault("schema_assessment", schema_assessment)
        metadata.setdefault("structural_evidence", structural_evidence)
        if not metadata.get("physical_grid_cells"):
            metadata["physical_grid_cells"] = dict(
                grid_cells_by_row.get(int(row.source_row), {})
            )
        metadata.setdefault(
            "physical_row_refs",
            [{"table": table.ref.as_dict(), "row_index": row.source_row}],
        )
        metadata["provider"] = provider_key
    result.semantic_authoritative = True
    return output


__all__ = [
    "StructuredReconstructionResult",
    "project_semantic_table",
    "semantic_table_authoritative_enabled",
]
