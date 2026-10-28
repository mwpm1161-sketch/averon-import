"""Loss-preserving conversion and offline multi-page draft assembly."""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any, Iterable

from averon_import.services.ai_first.contracts import (
    AICell, AIColumn, AIDocumentDraft, AIHandwritingMark, AIPageExtraction,
    AIRow, AITable, AIReviewIssue,
)

_HEADER_ALIASES = {
    "position": ("позиция", "поз.", "поз", "№", "№ п/п", "номер позиции"),
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
    for field, aliases in _HEADER_ALIASES.items() for alias in aliases
}
_POSITIVE_INTEGER = re.compile(r"^[1-9]\d*$")
_POSITION_IDENTIFIER = re.compile(r"^[1-9]\d*(?:[.-][1-9]\d*){0,5}$")


class AIInputError(ValueError):
    """Safe validation error that deliberately excludes source text."""


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise AIInputError("invalid_transcription_structure")
    return value


def _list(value: Any, *, default: list[Any] | None = None) -> list[Any]:
    if value is None and default is not None:
        return default
    if not isinstance(value, list):
        raise AIInputError("invalid_transcription_structure")
    return value


def _text(value: Any, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise AIInputError("invalid_transcription_structure")
    return value


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if type(value) is not bool:
        raise AIInputError("invalid_transcription_structure")
    return value


def _optional_index(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise AIInputError("invalid_transcription_structure")
    return value


def _marks(raw_marks: Any, scope: str) -> list[AIHandwritingMark] | None:
    if raw_marks is None:
        return None
    marks = []
    for raw in _list(raw_marks):
        item = _mapping(raw)
        marks.append(AIHandwritingMark(
            text=_text(item.get("text"), nullable=True),
            scope=scope,
            bbox_candidate=item.get("bbox_candidate"),
            raw_model_payload=dict(item),
        ))
    return marks


def _canonical_header(header: str | None) -> str | None:
    if header is None:
        return None
    return _HEADER_MAP.get(" ".join(header.casefold().split()))


def page_from_payload(
    payload: dict[str, Any], *, source_document_id: str,
    page_number: int | None = None,
) -> AIPageExtraction:
    """Convert a parsed model transcription without dropping unknown JSON fields.

    No geometry is required. Unknown table/row/cell fields remain in each
    raw_model_payload; the original JSON value is also retained at page level.
    """
    raw_page = _mapping(payload)
    payload_number = raw_page.get("page")
    if page_number is None:
        number = payload_number
    else:
        if "page" in raw_page and (type(payload_number) is not int or payload_number != page_number):
            raise AIInputError("page_identity_mismatch")
        number = page_number
    if type(number) is not int or number < 1 or not source_document_id:
        raise AIInputError("invalid_page_identity")
    tables_payload = _list(raw_page.get("tables"), default=[])
    page_fingerprint = _digest(raw_page)
    page_prefix = f"doc:{source_document_id}:page:{number}:rev:{page_fingerprint[:16]}"
    tables: list[AITable] = []
    page_issues: list[AIReviewIssue] = []

    for table_index, table_value in enumerate(tables_payload):
        raw_table = _mapping(table_value)
        table_id = f"{page_prefix}:table:{table_index}"
        headers_raw = _list(raw_table.get("headers"), default=[])
        headers = [_text(value) for value in headers_raw]
        columns = [AIColumn(
            id=f"{table_id}:column:{column_index}",
            source_document_id=source_document_id,
            page_number=number,
            table_id=table_id,
            index=column_index,
            raw_header=header,
            canonical_field_candidate=_canonical_header(header),
            raw_model_payload={"raw_header": header},
        ) for column_index, header in enumerate(headers)]
        rows: list[AIRow] = []
        for row_index, row_value in enumerate(_list(raw_table.get("rows"), default=[])):
            raw_row = _mapping(row_value)
            row_id = f"{table_id}:row:{row_index}"
            cells: list[AICell] = []
            for cell_index, cell_value in enumerate(_list(raw_row.get("cells"), default=[])):
                raw_cell = _mapping(cell_value)
                column_index = raw_cell.get("column_index")
                if type(column_index) is not int or column_index < 0:
                    raise AIInputError("invalid_cell_address")
                raw_alternatives = raw_cell.get("alternatives")
                alternatives = None if raw_alternatives is None else [
                    _text(value) for value in _list(raw_alternatives)
                ]
                raw_replacements = raw_cell.get("handwritten_replacements")
                replacements = _marks(raw_replacements, "cell")
                cells.append(AICell(
                    id=f"{row_id}:cell:{cell_index}",
                    source_document_id=source_document_id,
                    page_number=number,
                    table_id=table_id,
                    row_id=row_id,
                    column_index=column_index,
                    raw_value=_text(raw_cell.get("raw_text"), nullable=True),
                    alternatives=alternatives,
                    unreadable=_optional_bool(raw_cell.get("unreadable")),
                    struck_through=_optional_bool(raw_cell.get("struck_through")),
                    handwritten=_optional_bool(raw_cell.get("handwritten")),
                    handwritten_replacements=replacements,
                    row_span=_optional_index(raw_cell.get("row_span")),
                    column_span=_optional_index(raw_cell.get("column_span")),
                    bbox_candidate=raw_cell.get("bbox_candidate"),
                    raw_model_payload=dict(raw_cell),
                ))
            kind = raw_row.get("kind")
            if kind is not None and not isinstance(kind, str):
                raise AIInputError("invalid_transcription_structure")
            row = AIRow(
                id=row_id,
                source_document_id=source_document_id,
                page_number=number,
                table_id=table_id,
                index=row_index,
                model_kind=kind,
                # Deliberately unclassified: model_kind is preserved as untrusted input.
                server_role_candidate=None,
                role_verified=False,
                raw_cells=cells,
                struck_through=_optional_bool(raw_row.get("struck_through")),
                handwritten=_optional_bool(raw_row.get("handwritten")),
                handwritten_replacements=_marks(raw_row.get("handwritten_replacements"), "row"),
                continuation_of=_optional_index(raw_row.get("continuation_of")),
                continues_previous_page=_optional_bool(raw_row.get("continues_previous_page")),
                bbox_candidate=raw_row.get("bbox_candidate"),
                raw_model_payload=dict(raw_row),
            )
            rows.append(row)
            uncertain_cells = [
                cell.id for cell in cells
                if cell.unreadable is True or cell.struck_through is True
                or cell.handwritten is True or bool(cell.alternatives)
                or bool(cell.handwritten_replacements)
            ]
            if (uncertain_cells or row.struck_through is True or row.handwritten is True
                    or bool(row.handwritten_replacements)):
                page_issues.append(_issue(
                    source_document_id, "row_has_uncertain_visual_evidence", "review", number,
                    [row.id, *uncertain_cells],
                    "Strike-through, handwriting, unreadable text, or alternate readings need human review.",
                ))
        table = AITable(
            id=table_id,
            source_document_id=source_document_id,
            page_number=number,
            index=table_index,
            columns=columns,
            rows=rows,
            continues_previous_page=_optional_bool(raw_table.get("continues_previous_page")),
            bbox_candidate=raw_table.get("bbox_candidate"),
            raw_model_payload=dict(raw_table),
        )
        tables.append(table)
        if table.continues_previous_page or any(
            row.continues_previous_page or row.continuation_of is not None for row in rows
        ):
            page_issues.append(_issue(
                source_document_id, "continuation_unverified", "review", number, [table_id],
                "Continuation is a model candidate and has not been joined to another page.",
            ))
        page_issues.extend(_table_candidate_issues(table))

    fragments = [_text(value) for value in _list(raw_page.get("unreadable_fragments"), default=[])]
    page_marks = _marks(raw_page.get("handwritten_replacements"), "page") or []
    if fragments:
        page_issues.append(_issue(
            source_document_id, "unreadable_page_fragment", "blocking", number, [],
            "The page contains unreadable source fragments.",
        ))
    if page_marks:
        page_issues.append(_issue(
            source_document_id, "page_handwriting_unlocalized", "review", number, [],
            "Page-level handwriting is not attributed to a row or cell.",
        ))
    if tables:
        status = "extracted"
    elif fragments or page_marks:
        status = "incomplete"
    else:
        status = "no_specification"
    return AIPageExtraction(
        source_document_id=source_document_id,
        page_number=number,
        extraction_fingerprint=page_fingerprint,
        status=status,
        tables=tables,
        unreadable_fragments=fragments,
        page_handwriting=page_marks,
        issues=page_issues,
        raw_model_payload=dict(raw_page),
    )


def failed_page(source_document_id: str, page_number: int, error_code: str) -> AIPageExtraction:
    if not source_document_id or type(page_number) is not int or page_number < 1 or not error_code:
        raise AIInputError("invalid_page_failure")
    raw = {"page": page_number, "error_code": error_code}
    fingerprint = _digest(raw)
    issue = _issue(source_document_id, "page_extraction_failed", "blocking", page_number, [], "Page extraction failed.")
    return AIPageExtraction(
        source_document_id=source_document_id,
        page_number=page_number,
        extraction_fingerprint=fingerprint,
        status="failed",
        error_code=error_code,
        issues=[issue],
        raw_model_payload={},
    )


def _issue(source_document_id: str, code: str, severity: str, page: int, record_ids: list[str], detail: str) -> AIReviewIssue:
    issue_id = f"issue:{_digest([code, page, record_ids, detail])[:20]}"
    return AIReviewIssue(
        id=issue_id, source_document_id=source_document_id, code=code, severity=severity, page_number=page,
        record_ids=record_ids, detail=detail,
    )


def _candidate_position(row: AIRow, position_column: int, name_column: int) -> str | None:
    values = {cell.column_index: cell.raw_value for cell in row.raw_cells}
    position = values.get(position_column)
    name = values.get(name_column)
    if not isinstance(position, str) or not _POSITION_IDENTIFIER.fullmatch(position.strip()):
        return None
    if not isinstance(name, str) or not name.strip() or _POSITIVE_INTEGER.fullmatch(name.strip()):
        return None
    return position.strip()


def _table_candidate_issues(table: AITable) -> list[AIReviewIssue]:
    by_field: dict[str, list[int]] = defaultdict(list)
    for column in table.columns:
        if column.canonical_field_candidate:
            by_field[column.canonical_field_candidate].append(column.index)
    issues: list[AIReviewIssue] = []
    # A duplicated or unknown header is insufficient evidence for position
    # numbering checks, but independent source-cell warnings remain useful.
    numbered: list[tuple[AIRow, str]] = []
    if len(by_field.get("position", [])) == 1 and len(by_field.get("name", [])) == 1:
        position_column, name_column = by_field["position"][0], by_field["name"][0]
        numbered = [
            (row, number) for row in table.rows
            if (number := _candidate_position(row, position_column, name_column)) is not None
        ]
    occurrences: dict[str, list[str]] = defaultdict(list)
    for row, number in numbered:
        occurrences[number].append(row.id)
    for number, row_ids in occurrences.items():
        if len(row_ids) > 1:
            issues.append(_issue(
                table.source_document_id, "duplicate_position_candidate", "review", table.page_number, row_ids,
                f"Position number {number} appears in multiple candidate rows; inspect source page.",
            ))
    integer_numbers = sorted(int(number) for number in occurrences if _POSITIVE_INTEGER.fullmatch(number))
    if any(right - left > 1 for left, right in zip(integer_numbers, integer_numbers[1:])):
        issues.append(_issue(
            table.source_document_id, "position_gap_candidate", "review", table.page_number,
            [row.id for row, _ in numbered],
            "A gap in candidate position numbers may indicate a missing or split row; inspect source page.",
        ))

    # Conservative source-column warning only; never move or rewrite values.
    columns_by_index = {column.index: column for column in table.columns}
    for row in table.rows:
        cells_by_index = {cell.column_index: cell for cell in row.raw_cells}
        if sum(bool((cell.raw_value or "").strip()) for cell in row.raw_cells) < 2:
            continue
        for column_index, cell in cells_by_index.items():
            column = columns_by_index.get(column_index)
            field = column.canonical_field_candidate if column else None
            value = (cell.raw_value or "").strip()
            words = re.findall(r"\w+", value, flags=re.UNICODE)
            suspicious = (
                field == "code" and (len(words) >= 6 or len(value) >= 72)
                or field == "quantity" and (len(words) >= 4 or len(value) >= 48)
                or field == "unit" and (len(words) >= 4 or len(value) >= 32)
            )
            if suspicious:
                issues.append(_issue(
                    table.source_document_id, "suspicious_column_shift_candidate", "review", table.page_number,
                    [table.id, row.id, cell.id],
                    "A value looks unusually long for its source column. Compare this row with the PDF; no values were moved.",
                ))
    return issues


def assemble_document_draft(
    source_document_id: str,
    pages: Iterable[AIPageExtraction],
    *,
    requested_pages: Iterable[int] | None = None,
    source_envelope: dict[str, Any] | None = None,
) -> AIDocumentDraft:
    """Build one ordered draft. Page continuations remain separate observations."""
    if not source_document_id:
        raise AIInputError("invalid_document_identity")
    by_number: dict[int, AIPageExtraction] = {}
    for page in pages:
        if page.source_document_id != source_document_id or page.page_number in by_number:
            raise AIInputError("duplicate_or_mismatched_page")
        by_number[page.page_number] = page
    requested = None if requested_pages is None else list(requested_pages)
    if requested is not None:
        if any(type(number) is not int or number < 1 for number in requested) or len(set(requested)) != len(requested):
            raise AIInputError("invalid_requested_pages")
        for number in sorted(set(requested) - set(by_number)):
            by_number[number] = failed_page(source_document_id, number, "page_result_missing")
        if set(by_number) - set(requested):
            raise AIInputError("unexpected_page_result")
    expected_page_numbers = sorted(requested) if requested is not None else []
    ordered_pages = [by_number[number] for number in sorted(by_number)]
    issues = [issue for page in ordered_pages for issue in page.issues]
    for page in ordered_pages:
        if page.status == "incomplete":
            issues.append(_issue(
                source_document_id, "page_transcription_incomplete", "blocking", page.page_number, [],
                "Page has unreadable or unlocalized evidence and needs human review.",
            ))
    revision_fingerprint = _digest({
        "source_document_id": source_document_id,
        "expected_page_numbers": expected_page_numbers,
        "pages": [[page.page_number, page.extraction_fingerprint, page.status] for page in ordered_pages],
    })
    return AIDocumentDraft(
        source_document_id=source_document_id,
        revision_id=f"rev:{revision_fingerprint[:24]}",
        pages=ordered_pages,
        expected_page_numbers=expected_page_numbers,
        issues=issues,
        source_envelope=dict(source_envelope or {}),
        extraction_is_verified=False,
        export_authorized=False,
    )
