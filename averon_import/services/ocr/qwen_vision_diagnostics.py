"""Offline structural diagnostics for isolated Qwen Vision pilot transcriptions.

These are review hypotheses only. They do not verify physical rows or authorize
export, Human Review completion, or sourcing.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from averon_import.services.ocr.qwen_vision_contract import VisionPage

_FIELD_ALIASES = {
    "position": ("позиция", "поз.", "поз", "№", "№ п/п"),
    "name": ("наименование", "наименование и техническая характеристика", "описание"),
    "type_mark": ("тип", "марка", "тип / марка / обозначение", "обозначение"),
    "code": ("код", "код оборудования", "артикул"),
    "manufacturer": ("производитель", "изготовитель", "завод-изготовитель"),
    "unit": ("единица измерения", "ед. изм.", "ед.изм.", "ед. изм"),
    "quantity": ("количество", "кол-во", "кол."),
    "mass": ("масса единицы, кг", "масса единицы", "масса"),
    "note": ("примечание", "примечания"),
}
_HEADER_MAP = {
    " ".join(alias.casefold().split()): field
    for field, aliases in _FIELD_ALIASES.items()
    for alias in aliases
}
_POSITION_RE = re.compile(r"^[1-9]\d*$")


def _norm(value: str) -> str:
    return " ".join(value.casefold().split())


def _column_fields(headers: list[str]) -> tuple[dict[int, str], set[str]]:
    proposed = {
        index: _HEADER_MAP.get(_norm(header))
        for index, header in enumerate(headers)
    }
    counts = Counter(field for field in proposed.values() if field)
    ambiguous = {field for field, count in counts.items() if count > 1}
    return ({index: field for index, field in proposed.items() if field}, ambiguous)


def _cell_map(row: Any) -> dict[int, Any]:
    return {cell.column_index: cell for cell in row.cells}


def _text(row: Any, index: int) -> str:
    cell = _cell_map(row).get(index)
    return cell.raw_text.strip() if cell else ""


def _is_numbered_position(row: Any, position_index: int, name_index: int) -> bool:
    position, name = _text(row, position_index), _text(row, name_index)
    return bool(_POSITION_RE.fullmatch(position) and name and not _POSITION_RE.fullmatch(name))


def _matches_column_numbering(row: Any, header_count: int) -> bool:
    if header_count < 3:
        return False
    cells = _cell_map(row)
    return (
        len(cells) == header_count
        and all(
            index in cells and cells[index].column_span == 1 and cells[index].row_span == 1
            and cells[index].raw_text.strip() == str(index + 1)
            for index in range(header_count)
        )
    )


def _matches_table_header(row: Any, headers: list[str]) -> bool:
    cells = _cell_map(row)
    matches = sum(
        1 for index, header in enumerate(headers)
        if index in cells and _norm(cells[index].raw_text) == _norm(header)
    )
    return matches >= min(2, len(headers)) and matches > 0


def _has_text(row: Any) -> bool:
    return any(
        cell.raw_text.strip() or cell.alternatives or cell.handwritten_replacements
        for cell in row.cells
    )


def _role_for_row(
    table: Any, row_index: int, position_index: int, name_index: int,
    numbered: list[bool],
) -> tuple[str, int | None, list[str]]:
    row = table.rows[row_index]
    position_text = _text(row, position_index)
    position_number = int(position_text) if _POSITION_RE.fullmatch(position_text) else None

    if _matches_column_numbering(row, len(table.headers)):
        return "column_numbering", None, ["cells enumerate table columns from 1"]
    if _matches_table_header(row, table.headers):
        return "table_header", None, ["cell text matches table column headers"]
    if _is_numbered_position(row, position_index, name_index):
        return "numbered_position", position_number, [
            "position-column cell is a positive integer",
            "name-column cell contains nonnumeric text",
        ]

    name_text = _text(row, name_index)
    adjacent_item = (
        (row_index > 0 and numbered[row_index - 1])
        or (row_index + 1 < len(numbered) and numbered[row_index + 1])
    )
    structural_text_cells = [
        cell for cell in row.cells
        if cell.raw_text.strip() and not cell.handwritten and not cell.handwritten_replacements
    ]
    position_cell = _cell_map(row).get(position_index)
    position_blank = not position_cell or not position_cell.raw_text.strip()
    if name_text and position_blank and adjacent_item and len(structural_text_cells) <= 1:
        return "section_header", None, [
            "position column is blank",
            "one non-handwritten text cell labels the section",
            "an adjacent row has position-and-name structure",
        ]

    if row.continues_previous_page or row.continuation_of is not None:
        return "continuation_candidate", None, ["continuation marker is present"]
    if _has_text(row):
        return "ambiguous", None, ["row has content without a strong structural pattern"]
    return "other", None, ["row has no readable cell content"]


def _model_role_conflict(model_kind: str, role: str) -> bool:
    expected = {
        "table_header": {"header"},
        "column_numbering": {"header"},
        "section_header": {"section"},
        "numbered_position": {"item", "component"},
        "continuation_candidate": {"continuation"},
    }.get(role)
    return expected is not None and model_kind not in expected


def _projection_diagnostics(
    row: Any, fields: dict[int, str], ambiguous: set[str],
    *, page_handwriting_unlocalized: bool,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    cell_results = []
    mapped_fields: set[str] = set()
    suppressed_fields: set[str] = set()
    for cell in row.cells:
        field = fields.get(cell.column_index)
        reasons: list[str] = []
        if field is None:
            status = "raw_only_unmapped_column" if cell.raw_text.strip() else "no_canonical_field"
        else:
            mapped_fields.add(field)
            if field in ambiguous:
                status = "suppressed_ambiguous_header"
                reasons.append("duplicate canonical headers are ambiguous")
                suppressed_fields.add(field)
            else:
                if page_handwriting_unlocalized:
                    reasons.append("page-level handwritten text has no row/cell attribution")
                if row.struck_through:
                    reasons.append("row is struck through")
                if row.handwritten or row.handwritten_replacements:
                    reasons.append("row-level handwriting suppresses projected values")
                if cell.struck_through:
                    reasons.append("cell is struck through")
                if cell.handwritten:
                    reasons.append("cell is marked handwritten")
                if cell.handwritten_replacements:
                    reasons.append("cell has a handwritten replacement")
                if cell.unreadable:
                    reasons.append("cell is unreadable")
                if cell.alternatives:
                    reasons.append("cell has competing OCR readings")
                if cell.column_span != 1 or cell.row_span != 1:
                    reasons.append("spanned cell is not projected")
                if reasons:
                    status = "suppressed_uncertain"
                    suppressed_fields.add(field)
                else:
                    status = "mapped_unchanged"
        cell_results.append({
            "column_index": cell.column_index,
            "header": None,
            "canonical_field": field,
            "status": status,
            "reasons": reasons,
        })
    return cell_results, sorted(mapped_fields), sorted(suppressed_fields)


def analyze_transcriptions(transcriptions: dict[int, VisionPage]) -> dict[str, Any]:
    """Classify candidate structure without changing transcription or granting trust."""
    rows: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    model_counts: Counter[str] = Counter()
    role_counts: Counter[str] = Counter()
    per_table: list[dict[str, Any]] = []

    for page_number, page in sorted(transcriptions.items()):
        page_handwriting_unlocalized = bool(page.handwritten_replacements)
        page_record = {
            "page": page_number,
            "unreadable_fragments": list(page.unreadable_fragments),
            "handwritten_replacements": [
                item.model_dump(mode="json") for item in page.handwritten_replacements
            ],
            "table_summaries": [],
        }
        for table_index, table in enumerate(page.tables):
            fields, ambiguous_fields = _column_fields(table.headers)
            safe_fields = {
                index: field for index, field in fields.items() if field not in ambiguous_fields
            }
            position_index = next(
                (index for index, field in safe_fields.items() if field == "position"), 0
            )
            name_index = next(
                (index for index, field in safe_fields.items() if field == "name"),
                min(position_index + 1, max(len(table.headers) - 1, 0)),
            )
            numbered = [
                _is_numbered_position(row, position_index, name_index)
                for row in table.rows
            ]
            position_numbers: list[int] = []
            local_roles: Counter[str] = Counter()
            local_model: Counter[str] = Counter()
            local_conflicts = 0

            for row_index, row in enumerate(table.rows):
                role, position_number, evidence = _role_for_row(
                    table, row_index, position_index, name_index, numbered,
                )
                conflict = _model_role_conflict(row.kind, role)
                projection_cells, mapped_fields, suppressed_fields = _projection_diagnostics(
                    row, fields, ambiguous_fields,
                    page_handwriting_unlocalized=page_handwriting_unlocalized,
                )
                projection_by_index = {item["column_index"]: item for item in projection_cells}
                values: dict[str, str] = {}
                cells = []
                for cell in row.cells:
                    canonical = fields.get(cell.column_index)
                    if canonical and canonical not in ambiguous_fields and cell.column_span == 1 and cell.row_span == 1:
                        values[canonical] = cell.raw_text
                    cells.append({
                        "column_index": cell.column_index,
                        "column_span": cell.column_span,
                        "row_span": cell.row_span,
                        "header": table.headers[cell.column_index]
                        if cell.column_index < len(table.headers) else None,
                        "raw_text": cell.raw_text,
                        "alternatives": list(cell.alternatives),
                        "handwritten_replacements": [
                            item.model_dump(mode="json") for item in cell.handwritten_replacements
                        ],
                        "unreadable": cell.unreadable,
                        "struck_through": cell.struck_through,
                        "handwritten": cell.handwritten,
                        "bbox_candidate": (
                            cell.bbox_candidate.model_dump(mode="json") if cell.bbox_candidate else None
                        ),
                        "projection": projection_by_index.get(cell.column_index),
                    })
                raw_position = _text(row, position_index)
                record = {
                    "page": page_number,
                    "table_index": table_index,
                    "row_index": row_index,
                    "model_kind": row.kind,
                    "server_role_candidate": role,
                    "role_conflict": conflict,
                    "role_verified": False,
                    "position_label": raw_position or None,
                    "position_number": position_number,
                    "role_evidence": evidence,
                    "row_flags": {
                        "struck_through": row.struck_through,
                        "handwritten": row.handwritten,
                        "handwritten_replacements": [
                            item.model_dump(mode="json") for item in row.handwritten_replacements
                        ],
                        "continues_previous_page": row.continues_previous_page,
                        "continuation_of": row.continuation_of,
                    },
                    "values": values,
                    "cells": cells,
                    "projection": {
                        "model_kind_used_as_projection_gate": False,
                        "server_role_candidate_used_as_projection_gate": False,
                        "diagnostic_is_admission": False,
                        "mapped_fields": mapped_fields,
                        "suppressed_fields": suppressed_fields,
                        "raw_table_headers_preserved_in_transcription": True,
                        "unmapped_or_spanned_cell_count": sum(
                            cell["projection"]["status"] in {
                                "raw_only_unmapped_column", "suppressed_ambiguous_header",
                            }
                            for cell in cells
                        ),
                    },
                }
                rows.append(record)
                model_counts[row.kind] += 1
                role_counts[role] += 1
                local_model[row.kind] += 1
                local_roles[role] += 1
                local_conflicts += int(conflict)
                if role == "numbered_position" and position_number is not None:
                    position_numbers.append(position_number)

            frequency = Counter(position_numbers)
            unique_numbers = sorted(frequency)
            gaps = (
                [value for value in range(unique_numbers[0], unique_numbers[-1] + 1)
                 if value not in frequency]
                if unique_numbers else []
            )
            duplicates = sorted(value for value, count in frequency.items() if count > 1)
            table_summary = {
                "page": page_number,
                "table_index": table_index,
                "source_row_count": len(table.rows),
                "model_kind_counts": dict(sorted(local_model.items())),
                "server_role_candidate_counts": dict(sorted(local_roles.items())),
                "potential_numbered_position_count": local_roles["numbered_position"],
                "ambiguous_row_count": local_roles["ambiguous"],
                "confirmed_position_count": 0,
                "role_conflict_count": local_conflicts,
                "observed_position_numbers": unique_numbers,
                "missing_between_observed_positions": gaps,
                "duplicate_position_numbers": duplicates,
                "interpretation": "Structural hypotheses only; no role is physically verified.",
            }
            page_record["table_summaries"].append(table_summary)
            per_table.append(table_summary)
        pages.append(page_record)

    old_kind_candidates = sum(
        row["model_kind"] in {"item", "component", "continuation", "unknown"}
        for row in rows
    )
    summary = {
        "source_row_count": len(rows),
        "model_kind_counts": dict(sorted(model_counts.items())),
        "server_role_candidate_counts": dict(sorted(role_counts.items())),
        "model_kind_candidate_row_count": old_kind_candidates,
        "potential_numbered_position_count": role_counts["numbered_position"],
        "potential_continuation_count": role_counts["continuation_candidate"],
        "ambiguous_row_count": role_counts["ambiguous"],
        "confirmed_position_count": 0,
        "role_conflict_count": sum(row["role_conflict"] for row in rows),
        "position_sequences": per_table,
        "interpretation": (
            "Candidate roles are server-side hypotheses only. role_verified is false "
            "for every row; this analysis never grants USABLE or sourcing admission."
        ),
    }
    return {"summary": summary, "pages": pages, "rows": rows}
