"""Offline quality metrics for AI-first drafts against explicit references.

This module is not called by the production OCR route. It reports aggregate
counts only and never rewrites candidate text or grants review/export access.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from averon_import.services.ai_first.contracts import AIDocumentDraft, AIRow, AITable


_FIELD_ALIASES = {"article": "code"}
_REFERENCE_FIELDS = {
    "position", "code", "name", "quantity", "unit", "mass", "note",
    "type_mark", "manufacturer",
}


@dataclass(frozen=True, slots=True)
class ExpectedRow:
    page_number: int
    table_index: int
    row_index: int
    role: str
    fields: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class QualityReference:
    case_id: str
    expected_page_numbers: tuple[int, ...]
    expected_column_fields: Mapping[tuple[int, int], tuple[str | None, ...]]
    expected_rows: tuple[ExpectedRow, ...]


def reference_from_mapping(value: Mapping[str, Any]) -> QualityReference:
    """Parse a small JSON-compatible reference while keeping errors content-free."""
    try:
        case_id = value["case_id"]
        pages = value["expected_page_numbers"]
        columns = value["expected_columns"]
        rows = value["expected_rows"]
        if not isinstance(case_id, str) or not case_id or not isinstance(pages, list):
            raise ValueError
        if any(type(page) is not int or page < 1 for page in pages) or pages != sorted(set(pages)):
            raise ValueError
        expected_columns: dict[tuple[int, int], tuple[str | None, ...]] = {}
        for item in columns:
            page_number, table_index, fields = item["page_number"], item["table_index"], item["fields"]
            if type(page_number) is not int or page_number not in pages:
                raise ValueError
            if type(table_index) is not int or table_index < 0 or not isinstance(fields, list):
                raise ValueError
            if any(field is not None and not isinstance(field, str) for field in fields):
                raise ValueError
            if any(
                field is not None and _FIELD_ALIASES.get(field, field) not in _REFERENCE_FIELDS
                for field in fields
            ):
                raise ValueError
            normalized_fields = tuple(_FIELD_ALIASES.get(field, field) if field else None for field in fields)
            present_fields = [field for field in normalized_fields if field is not None]
            if len(present_fields) != len(set(present_fields)):
                raise ValueError
            key = (page_number, table_index)
            if key in expected_columns:
                raise ValueError
            expected_columns[key] = normalized_fields
        expected_rows: list[ExpectedRow] = []
        seen_rows: set[tuple[int, int, int]] = set()
        for item in rows:
            page_number, table_index, row_index = item["page_number"], item["table_index"], item["row_index"]
            role, fields = item["role"], item["fields"]
            key = (page_number, table_index, row_index)
            if (
                type(page_number) is not int or page_number not in pages
                or type(table_index) is not int or table_index < 0
                or type(row_index) is not int or row_index < 0
                or not isinstance(role, str) or not role
                or not isinstance(fields, dict)
                or any(not isinstance(name, str) or not isinstance(text, str) for name, text in fields.items())
                or any(_FIELD_ALIASES.get(name, name) not in _REFERENCE_FIELDS for name in fields)
                or key in seen_rows
            ):
                raise ValueError
            seen_rows.add(key)
            normalized = {_FIELD_ALIASES.get(name, name): text for name, text in fields.items()}
            expected_rows.append(ExpectedRow(page_number, table_index, row_index, role, normalized))
        if any((row.page_number, row.table_index) not in expected_columns for row in expected_rows):
            raise ValueError
        expected_rows.sort(key=lambda row: (row.page_number, row.table_index, row.row_index))
        positions = [
            (row.page_number, row.table_index, row.fields.get("position"))
            for row in expected_rows if row.role == "item"
        ]
        if any(position is None for _, _, position in positions) or len(positions) != len(set(positions)):
            raise ValueError
        return QualityReference(case_id, tuple(pages), expected_columns, tuple(expected_rows))
    except (KeyError, TypeError, ValueError):
        raise ValueError("invalid_quality_reference") from None


def _table_map(draft: AIDocumentDraft) -> dict[tuple[int, int], AITable]:
    return {
        (page.page_number, table.index): table
        for page in draft.pages for table in page.tables
    }


def _row_map(draft: AIDocumentDraft) -> dict[tuple[int, int, int], AIRow]:
    return {
        (page.page_number, table.index, row.index): row
        for page in draft.pages for table in page.tables for row in table.rows
    }


def _field_value(table: AITable | None, row: AIRow | None, field: str) -> str | None:
    if table is None or row is None:
        return None
    indexes = [column.index for column in table.columns if column.canonical_field_candidate == field]
    if len(indexes) != 1:
        return None
    cells = [cell for cell in row.raw_cells if cell.column_index == indexes[0]]
    if len(cells) != 1:
        return None
    return cells[0].raw_value


def evaluate_draft_quality(reference: QualityReference, draft: AIDocumentDraft) -> dict[str, Any]:
    """Compare raw observations with gold values without changing the draft.

    Exact string comparison is intentional: normalization and OCR correction
    belong in a separately reviewed evaluation protocol, not in this scorer.
    Returned metrics contain counts and booleans, never source values.
    """
    tables = _table_map(draft)
    rows = _row_map(draft)
    pages = {page.page_number: page for page in draft.pages}
    expected_items = [row for row in reference.expected_rows if row.role == "item"]
    expected_rows_by_location = {
        (row.page_number, row.table_index, row.row_index): row
        for row in reference.expected_rows
    }
    expected_items_by_location = {
        (row.page_number, row.table_index, row.row_index): row
        for row in expected_items
    }
    expected_positions = {
        (row.page_number, row.table_index, row.fields["position"])
        for row in expected_items
    }

    header_assignment_errors = 0
    for key, expected_fields in reference.expected_column_fields.items():
        table = tables.get(key)
        actual_fields = [] if table is None else [None] * (max((column.index for column in table.columns), default=-1) + 1)
        if table is not None:
            for column in table.columns:
                if column.index < len(actual_fields):
                    actual_fields[column.index] = column.canonical_field_candidate
        limit = max(len(expected_fields), len(actual_fields))
        header_assignment_errors += sum(
            index >= len(expected_fields) or index >= len(actual_fields)
            or expected_fields[index] != actual_fields[index]
            for index in range(limit)
        )

    position_alignment_errors = 0
    article_errors = quantity_errors = unit_errors = name_errors = 0
    mass_errors = note_errors = type_mark_errors = manufacturer_errors = 0
    classification_errors = missing_rows = cell_column_assignment_errors = 0
    for expected in reference.expected_rows:
        key = (expected.page_number, expected.table_index, expected.row_index)
        row = rows.get(key)
        table = tables.get((expected.page_number, expected.table_index))
        if row is None:
            missing_rows += 1
            if expected.role == "item":
                position_alignment_errors += 1
            continue
        if row.model_kind != expected.role:
            classification_errors += 1
        expected_columns = reference.expected_column_fields[(expected.page_number, expected.table_index)]
        expected_index_by_field = {
            field: index for index, field in enumerate(expected_columns) if field is not None
        }
        for field, expected_value in expected.fields.items():
            actual_value = _field_value(table, row, field)
            expected_index = expected_index_by_field.get(field)
            if expected_value and expected_index is not None:
                matching_cells = [cell for cell in row.raw_cells if cell.raw_value == expected_value]
                if len(matching_cells) == 1 and matching_cells[0].column_index != expected_index:
                    cell_column_assignment_errors += 1
            if actual_value == expected_value:
                continue
            if field == "position":
                position_alignment_errors += 1
            elif field == "code":
                article_errors += 1
            elif field == "quantity":
                quantity_errors += 1
            elif field == "unit":
                unit_errors += 1
            elif field == "name":
                name_errors += 1
            elif field == "mass":
                mass_errors += 1
            elif field == "note":
                note_errors += 1
            elif field == "type_mark":
                type_mark_errors += 1
            elif field == "manufacturer":
                manufacturer_errors += 1
    extra_rows = sum(
        key not in {(row.page_number, row.table_index, row.row_index) for row in reference.expected_rows}
        for key in rows
    )
    # Only rows that are actually classified as items can contribute item
    # candidates. If a physical reference row is known to be a header/section,
    # exclude it even when the model mislabeled it as an item: its numeric
    # contents must not make an absent position appear covered.
    observed_positions: list[tuple[int, int, int, str]] = []
    for key, row in rows.items():
        expected_at_location = expected_rows_by_location.get(key)
        if row.model_kind != "item" or (
            expected_at_location is not None and expected_at_location.role != "item"
        ):
            continue
        page_number, table_index, _ = key
        value = _field_value(tables.get((page_number, table_index)), row, "position")
        if value not in (None, ""):
            observed_positions.append((*key, value))
    observed_positions.sort(key=lambda item: item[:3])
    observed_sequence = [item[3] for item in observed_positions]
    expected_sequence = [row.fields["position"] for row in expected_items]
    order_preserved = observed_sequence == expected_sequence
    observed_position_counts: dict[tuple[int, int, str], int] = {}
    for page_number, table_index, _, position in observed_positions:
        key = (page_number, table_index, position)
        observed_position_counts[key] = observed_position_counts.get(key, 0) + 1
    # Coverage requires the expected value at its expected physical item row.
    # A matching number on another row (including an extra model item) cannot
    # compensate for an omitted or misclassified position.
    recognized_locations = {
        key
        for key, expected in expected_items_by_location.items()
        if (row := rows.get(key)) is not None
        and row.model_kind == "item"
        and _field_value(tables.get((key[0], key[1])), row, "position")
        == expected.fields["position"]
    }
    recognized_positions = len(recognized_locations)
    duplicate_positions = sum(
        count - 1 for key, count in observed_position_counts.items()
        if key in expected_positions and count > 1
    )

    extra_item_rows = sum(
        row.model_kind == "item"
        and (
            key not in expected_items_by_location
            or _field_value(tables.get((key[0], key[1])), row, "position")
            != expected_items_by_location[key].fields["position"]
        )
        for key, row in rows.items()
    )
    column_assignment_errors = header_assignment_errors + cell_column_assignment_errors
    missing_pages = sum(number not in pages for number in reference.expected_page_numbers)
    incomplete_pages = sum(
        number in pages and pages[number].status in {"failed", "incomplete"}
        for number in reference.expected_page_numbers
    )
    pages_with_all_positions = 0
    for number in reference.expected_page_numbers:
        if number not in pages or pages[number].status in {"failed", "incomplete"}:
            continue
        expected_page_items = [row for row in expected_items if row.page_number == number]
        if all(
            (row.page_number, row.table_index, row.row_index) in recognized_locations
            for row in expected_page_items
        ):
            pages_with_all_positions += 1

    expected_count = len(expected_items)
    return {
        "case_id": reference.case_id,
        "expected_pages": len(reference.expected_page_numbers),
        "present_pages": len(set(reference.expected_page_numbers) & set(pages)),
        "missing_pages": missing_pages,
        "missing_rows": missing_rows,
        "incomplete_pages": incomplete_pages,
        "pages_with_all_positions": pages_with_all_positions,
        "page_completeness_rate": (
            pages_with_all_positions / len(reference.expected_page_numbers)
            if reference.expected_page_numbers else None
        ),
        "expected_positions": expected_count,
        "recognized_positions": recognized_positions,
        "missing_positions": expected_count - recognized_positions,
        "position_coverage_rate": recognized_positions / expected_count if expected_count else None,
        "position_alignment_errors": position_alignment_errors,
        "position_errors": position_alignment_errors,
        "duplicate_positions": duplicate_positions,
        "unexpected_positions": sum(
            count for key, count in observed_position_counts.items() if key not in expected_positions
        ),
        "position_order_preserved": order_preserved,
        "position_order_errors": 0 if order_preserved else 1,
        "extra_rows": extra_rows,
        "extra_item_rows": extra_item_rows,
        "article_errors": article_errors,
        "name_errors": name_errors,
        "mass_errors": mass_errors,
        "note_errors": note_errors,
        "type_mark_errors": type_mark_errors,
        "manufacturer_errors": manufacturer_errors,
        "quantity_errors": quantity_errors,
        "unit_errors": unit_errors,
        "column_assignment_errors": column_assignment_errors,
        "header_assignment_errors": header_assignment_errors,
        "cell_column_assignment_errors": cell_column_assignment_errors,
        "classification_errors": classification_errors,
    }
