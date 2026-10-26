"""Explicitly experimental XLSX for local AI-first estimator pilots.

This exporter is intentionally separate from the production export service and
does not authorize sourcing or a commercial estimate. It always includes a
loss-preserving review sheet and a page-coverage sheet.
"""
from __future__ import annotations

from collections import defaultdict
from io import BytesIO
import json
import re
from typing import Any, Iterable, Mapping, Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from averon_import.services.ai_first.contracts import AIDocumentDraft, AIReviewState


MAX_PILOT_EXPORT_PAGES = 3
MAX_EXCEL_CELL_CHARS = 32767
MAX_EXCEL_COLUMNS = 16384
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

ROLE_LABELS = {
    "header": "Заголовок",
    "column_numbering": "Нумерация колонок",
    "section": "Раздел",
    "standalone_item": "Самостоятельная позиция",
    "component": "Компонент комплекта",
    "continuation": "Продолжение строки",
    "note": "Примечание",
    "unknown": "Не определена",
}
DISPOSITION_LABELS = {
    "confirmed": "Подтверждена",
    "corrected": "Исправлена и подтверждена",
    "excluded": "Исключена",
}
CANONICAL_FIELDS = (
    ("position_number", "№ позиции"),
    ("name", "Наименование"),
    ("brand", "Марка"),
    ("manufacturer", "Производитель"),
    ("article", "Артикул / обозначение"),
    ("unit", "Единица измерения"),
    ("quantity", "Количество"),
    ("notes", "Примечания"),
)

_HEADER_ALIASES = {
    "position_number": {"номер", "номер позиции", "поз", "позиция", "№ п п", "п п"},
    "name": {"наименование", "название", "наименование продукции", "наименование оборудования", "наименование материала"},
    "brand": {"марка", "марка тип", "марка оборудования", "марка материала"},
    "manufacturer": {"производитель", "изготовитель", "производитель оборудования"},
    "article": {"артикул", "код", "обозначение", "артикул обозначение"},
    "unit": {"ед изм", "единица измерения", "единицы измерения", "ед", "ед изм"},
    "quantity": {"количество", "кол во", "кол", "qty"},
    "notes": {"примечание", "примечания", "комментарий", "особые указания"},
}


class PilotExportError(ValueError):
    """Safe pilot-export rejection code; never contains document data."""


def _header_key(value: object) -> str:
    text = str(value or "").casefold().replace("ё", "е").replace("№", " номер ")
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    return " ".join(text.split())


def _canonical_field(header: object) -> str | None:
    key = _header_key(header)
    for field, aliases in _HEADER_ALIASES.items():
        if key in aliases:
            return field
    return None


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _bounded(value: object) -> str:
    text = "" if value is None else str(value)
    if len(text) > MAX_EXCEL_CELL_CHARS:
        raise PilotExportError("export_cell_limit_exceeded")
    return text


def _safe_filename(value: object) -> str:
    # The workbook is downloaded by the authenticated local user; never expose a path.
    text = str(value or "document.pdf").replace("\\", "/").split("/")[-1]
    return text[:255] or "document.pdf"


def _issue_index(draft: AIDocumentDraft) -> dict[int, list[Any]]:
    by_page: dict[int, list[Any]] = defaultdict(list)
    seen: set[str] = set()
    for issue in [*draft.issues, *(item for page in draft.pages for item in page.issues)]:
        if issue.id in seen:
            continue
        seen.add(issue.id)
        if issue.page_number is not None:
            by_page[issue.page_number].append(issue)
        else:
            for page_number in draft.expected_page_numbers:
                by_page[page_number].append(issue)
    return by_page


def _active_lineage_sources(review: AIReviewState) -> set[str]:
    manual_by_id = {row.id: row for row in review.manual_rows}
    pending = [
        source_id
        for row in review.manual_rows if row.active
        for source_id in row.source_record_ids
    ]
    result: set[str] = set()
    while pending:
        record_id = pending.pop()
        if record_id in result:
            continue
        result.add(record_id)
        ancestor = manual_by_id.get(record_id)
        if ancestor is not None:
            pending.extend(ancestor.source_record_ids)
    return result


def _rows_for_document(draft: AIDocumentDraft, review: AIReviewState) -> list[dict[str, Any]]:
    role_records = review.row_roles
    decisions = review.decisions
    manual_ids = {item.id for item in review.manual_rows}
    superseded = _active_lineage_sources(review)
    result: list[dict[str, Any]] = []
    for page in draft.pages:
        for table in page.tables:
            # Keep the exact (including blank) source header. A display fallback
            # would erase the distinction between an empty header and a real one.
            columns = {item.index: item.raw_header or "" for item in table.columns}
            for row in table.rows:
                cells = sorted(row.raw_cells, key=lambda item: item.column_index)
                role = role_records.get(row.id)
                decision = decisions.get(row.id)
                result.append({
                    "id": row.id,
                    "page": page.page_number,
                    "page_status": page.status,
                    "table_id": table.id,
                    "table_index": table.index + 1,
                    "row_index": row.index + 1,
                    "model_kind": row.model_kind or "",
                    "role": role.role if role and role.active else "unknown",
                    "role_active": bool(role and role.active),
                    "parent_record_id": role.parent_record_id if role and role.active else "",
                    "decision": decision,
                    "active": True,
                    "manual": False,
                    "superseded": row.id in superseded,
                    "row": row,
                    "cells": cells,
                    "columns": columns,
                    "source_values": [
                        {"column_index": cell.column_index, "header": columns.get(cell.column_index, f"Столбец {cell.column_index + 1}"), "value": cell.raw_value or ""}
                        for cell in cells
                    ],
                    "current_values": [
                        {"column_index": cell.column_index, "header": columns.get(cell.column_index, ""), "value": (decision.values.get(f"cell:{cell.column_index}", cell.raw_value or "") if decision and (decision.active or decision.invalidation_reason == "role_change") else cell.raw_value or "")}
                        for cell in cells
                    ],
                    "last_saved_values": dict(decision.values) if decision else {},
                    "evidence": {
                        # Keep the per-row worksheet below Excel's 32,767 character
                        # cell limit. The complete, lossless page payload (including
                        # raw model fields) is chunked in "Исходная структура".
                        "row": {
                            "id": row.id,
                            "index": row.index,
                            "model_kind": row.model_kind,
                            "server_role_candidate": row.server_role_candidate,
                            "role_verified": row.role_verified,
                            "raw_cell_ids": [cell.id for cell in cells],
                            "struck_through": row.struck_through,
                            "handwritten": row.handwritten,
                            "continuation_of": row.continuation_of,
                            "continues_previous_page": row.continues_previous_page,
                            "bbox_candidate": row.bbox_candidate,
                        },
                        "table": {
                            "id": table.id,
                            "index": table.index,
                            "columns": [
                                {
                                    "id": item.id,
                                    "index": item.index,
                                    "raw_header": item.raw_header,
                                    "canonical_field_candidate": item.canonical_field_candidate,
                                }
                                for item in table.columns
                            ],
                            "continues_previous_page": table.continues_previous_page,
                            "bbox_candidate": table.bbox_candidate,
                        },
                        "full_raw_payload_sheet": "Исходная структура",
                    },
                })
    default_headers = ["Поз.", "Наименование", "Артикул", "Ед. изм.", "Количество", "Примечание"]
    for manual in review.manual_rows:
        if manual.page_number not in {page.page_number for page in draft.pages}:
            continue
        decision = decisions.get(manual.id)
        values = [
            {"column_index": index, "header": default_headers[index] if index < len(default_headers) else f"Столбец {index + 1}", "value": value}
            for index, value in sorted(
                ((int(key.split(":", 1)[1]), value) for key, value in manual.values.items() if key.startswith("cell:") and key.split(":", 1)[1].isdigit()),
                key=lambda item: item[0],
            )
        ]
        role = role_records.get(manual.id)
        page = next(item for item in draft.pages if item.page_number == manual.page_number)
        result.append({
            "id": manual.id,
            "page": manual.page_number,
            "page_status": page.status,
            "table_id": "",
            "table_index": "",
            "row_index": "",
            "model_kind": "manual_row",
            "role": role.role if role and role.active else "unknown",
            "role_active": bool(role and role.active),
            "parent_record_id": role.parent_record_id if role and role.active else "",
            "decision": decision,
            "active": manual.active,
            "manual": True,
            "superseded": manual.id in superseded,
            "row": manual,
            "cells": [],
            "columns": {item["column_index"]: item["header"] for item in values},
            "source_values": values,
            "current_values": [
                {**item, "value": (decision.values.get(f"cell:{item['column_index']}", item["value"]) if decision and (decision.active or decision.invalidation_reason == "role_change") else item["value"])}
                for item in values
            ],
            "last_saved_values": dict(decision.values) if decision else {},
            "evidence": {"manual_row": manual.model_dump(mode="json")},
        })
    return result


def _page_report_states(report: Mapping[str, Any]) -> dict[int, str]:
    result: dict[int, str] = {}
    for item in report.get("pages", []) if isinstance(report.get("pages", []), list) else []:
        if isinstance(item, Mapping) and type(item.get("page_number")) is int:
            result[item["page_number"]] = str(item.get("state") or "unknown")
    return result


def _page_report_errors(report: Mapping[str, Any]) -> dict[int, str]:
    result: dict[int, str] = {}
    for item in report.get("pages", []) if isinstance(report.get("pages", []), list) else []:
        if isinstance(item, Mapping) and type(item.get("page_number")) is int:
            attempts = item.get("attempts") if isinstance(item.get("attempts"), list) else []
            last_attempt = attempts[-1] if attempts and isinstance(attempts[-1], Mapping) else {}
            code = last_attempt.get("safe_error_code") or item.get("safe_error_code") or ""
            result[item["page_number"]] = str(code)
    return result


def _row_issue_open(row: Mapping[str, Any], issue_index: Mapping[int, list[Any]], review: AIReviewState) -> bool:
    record_id = str(row["id"])
    evidence_ids = {record_id}
    table_id = row.get("table_id")
    if table_id:
        evidence_ids.add(str(table_id))
    evidence_ids.update(cell.id for cell in row.get("cells", []))
    for issue in issue_index.get(int(row["page"]), []):
        if issue.severity not in {"review", "blocking"} or issue.id in review.resolved_issue_ids:
            continue
        if not issue.record_ids or evidence_ids.intersection(issue.record_ids):
            return True
    for cell in row.get("cells", []):
        uncertain = bool(cell.unreadable or cell.struck_through or cell.handwritten or cell.alternatives or cell.handwritten_replacements)
        if not uncertain:
            continue
        related_issue_ids = {
            issue.id for issue in issue_index.get(int(row["page"]), [])
            if issue.severity in {"review", "blocking"}
            and ({cell.id, record_id} & set(issue.record_ids))
        }
        if not related_issue_ids or any(issue_id not in review.resolved_issue_ids for issue_id in related_issue_ids):
            return True
    return False


def _values_by_field(row: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    labels = row["columns"]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    current = {item["column_index"]: item["value"] for item in row["current_values"]}
    for item in row["source_values"]:
        field = _canonical_field(labels.get(item["column_index"], item["header"]))
        if field:
            grouped[field].append({"raw": item["value"], "current": current.get(item["column_index"], item["value"])})
    return grouped


def _review_status(row: Mapping[str, Any], complete_page: bool, processing_state: str, open_issue: bool) -> str:
    decision = row.get("decision")
    if row.get("superseded"):
        return "Заменена структурным решением"
    if not row.get("active"):
        return "Неактивная ручная запись"
    if not row.get("role_active"):
        return "Роль строки не проверена"
    if not decision or not decision.active:
        return "Значения требуют проверки"
    if decision.disposition == "excluded":
        return "Исключена из проверяемой спецификации"
    if not complete_page:
        return "Страница не подтверждена полностью"
    if processing_state != "completed":
        return "Обработка страницы не завершена"
    if open_issue:
        return "Есть неразрешённое замечание"
    if row.get("ambiguous_field_mapping"):
        return "Неоднозначное сопоставление колонок"
    if row["role"] != "standalone_item":
        return "Не является самостоятельной позицией"
    return DISPOSITION_LABELS.get(decision.disposition, "Проверена")


def _write_sheet(
    sheet, title: str, document_name: str, document_id: str,
    selected_pages: Sequence[int], page_count: int,
    header: Sequence[str], rows: Iterable[Sequence[object]], widths: Sequence[int] | None = None,
    detail_header: Sequence[object] | None = None,
) -> None:
    coverage = "Полный охват страниц документа" if set(selected_pages) == set(range(1, page_count + 1)) else "ЧАСТИЧНЫЙ ОХВАТ ДОКУМЕНТА"
    sheet.sheet_view.showGridLines = False
    sheet.append(["ТЕСТ — AI-first Qwen Vision. Не является коммерческой сметой."])
    sheet.append([f"Документ: {document_name} · ID {document_id} · страницы {', '.join(map(str, selected_pages))} из {page_count} · {coverage}"])
    sheet.append([title])
    sheet.append([_bounded(value) for value in header])
    if detail_header is not None:
        sheet.append([_bounded(value) for value in detail_header])
    for values in rows:
        # openpyxl truncates long strings during append, so reject before it can
        # silently alter OCR content.
        sheet.append([_bounded(value) for value in values])
    for row in sheet.iter_rows():
        for cell in row:
            if cell.value is not None:
                value = str(cell.value)
                if len(value) > MAX_EXCEL_CELL_CHARS:
                    raise PilotExportError("export_cell_limit_exceeded")
                cell.value = value
                cell.data_type = "s"  # OCR text can never become an Excel formula.
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if cell.row == 1:
                cell.fill = PatternFill("solid", fgColor="FDE68A")
                cell.font = Font(bold=True, color="7C2D12")
            elif cell.row == 4:
                cell.fill = PatternFill("solid", fgColor="1D4ED8")
                cell.font = Font(bold=True, color="FFFFFF")
            elif detail_header is not None and cell.row == 5:
                cell.fill = PatternFill("solid", fgColor="DBEAFE")
                cell.font = Font(bold=True, color="1E3A8A")
    sheet.freeze_panes = "A6" if detail_header is not None else "A5"
    if detail_header is None and sheet.max_row >= 4 and sheet.max_column:
        sheet.auto_filter.ref = f"A4:{get_column_letter(sheet.max_column)}{sheet.max_row}"
    if widths:
        for index, width in enumerate(widths, start=1):
            sheet.column_dimensions[get_column_letter(index)].width = width
    else:
        for index in range(1, sheet.max_column + 1):
            heading = sheet.cell(4, index).value or ""
            source_heading = sheet.cell(5, index).value or "" if detail_header is not None else ""
            sheet.column_dimensions[get_column_letter(index)].width = min(42, max(14, len(str(heading)) + 2, min(40, len(str(source_heading)) + 2)))


def _source_sheet_name(page_number: int, table_index: int | str) -> str:
    suffix = "manual" if table_index == "manual" else f"T{int(table_index):03d}"
    return f"P{int(page_number):03d}-{suffix}"[:31]


def _source_table_sheet(
    workbook: Workbook,
    *,
    name: str,
    title: str,
    filename: str,
    document_id: str,
    selected: Sequence[int],
    page_count: int,
    columns: Sequence[tuple[int, str]],
    rows: Sequence[dict[str, Any]],
) -> dict[str, int]:
    """Write one source table with exact headers and visible raw/reviewed cells."""
    if len(columns) * 2 + 6 > MAX_EXCEL_COLUMNS:
        raise PilotExportError("export_column_limit_exceeded")
    sheet = workbook.create_sheet(name)
    fixed = ["Строка источника", "ID строки", "Тип по модели", "Роль Human Review", "Статус роли", "Статус проверки"]
    header = list(fixed)
    detail_header: list[object] = ["", "", "", "", "", ""]
    for index, source_header in columns:
        label = source_header if source_header else "[без заголовка]"
        header.extend([f"Индекс {index} · исходно · {label}", f"Индекс {index} · проверено · {label}"])
        # Repeated/blank labels are retained verbatim in the next visible row;
        # column indexes in row 4 keep their identities distinct.
        detail_header.extend([source_header, source_header])
    data: list[list[object]] = []
    location: dict[str, int] = {}
    for offset, row in enumerate(rows, start=6):
        location[str(row["id"])] = offset
        source_values = {int(item["column_index"]): item["value"] for item in row["source_values"]}
        current_values = {int(item["column_index"]): item["value"] for item in row["current_values"]}
        role = ROLE_LABELS.get(row["role"], "Не определена") if row["role_active"] else "Не подтверждена человеком"
        values: list[object] = [
            "Ручная" if row.get("manual") else row.get("row_index", ""),
            row["id"], row["model_kind"], role,
            "Подтверждена человеком" if row["role_active"] else "Не подтверждена",
            row["review_status"],
        ]
        for index, _source_header in columns:
            values.extend([source_values.get(index, ""), current_values.get(index, source_values.get(index, ""))])
        data.append(values)
    _write_sheet(
        sheet, title, filename, document_id, selected, page_count,
        header, data, detail_header=detail_header,
    )
    return location


def build_ai_first_pilot_xlsx(
    draft: AIDocumentDraft,
    review: AIReviewState,
    report: Mapping[str, Any],
    *,
    page_numbers: Sequence[int],
    document_name: str,
) -> bytes:
    """Build a local, explicitly test-marked workbook from saved review state."""
    if not page_numbers or len(page_numbers) > MAX_PILOT_EXPORT_PAGES:
        raise PilotExportError("invalid_page_scope")
    if any(type(number) is not int for number in page_numbers) or len(set(page_numbers)) != len(page_numbers):
        raise PilotExportError("invalid_page_scope")
    selected = sorted(page_numbers)
    expected = set(draft.expected_page_numbers)
    if not set(selected) <= expected:
        raise PilotExportError("page_outside_document")
    if draft.source_document_id != review.source_document_id or draft.revision_id != review.revision_id:
        raise PilotExportError("review_state_revision_mismatch")

    page_count = len(draft.expected_page_numbers)
    filename = _safe_filename(document_name)
    report_states = _page_report_states(report)
    confirmed_pages = set(review.confirmed_complete_pages)
    pages_by_number = {page.page_number: page for page in draft.pages}
    issue_index = _issue_index(draft)
    all_rows = [row for row in _rows_for_document(draft, review) if row["page"] in selected]
    selected_rows: list[dict[str, Any]] = []
    for row in all_rows:
        grouped = _values_by_field(row)
        row["ambiguous_field_mapping"] = any(len(values) > 1 for values in grouped.values())
        row["processing_state"] = report_states.get(int(row["page"]), "unknown")
        row["page_confirmed"] = int(row["page"]) in confirmed_pages
        row["open_issue"] = _row_issue_open(row, issue_index, review)
        row["review_status"] = _review_status(row, row["page_confirmed"], row["processing_state"], row["open_issue"])
        page = pages_by_number.get(int(row["page"]))
        row["verified_position"] = bool(
            row["active"] and not row["superseded"] and row["role_active"]
            and row["role"] == "standalone_item"
            and row["decision"] is not None and row["decision"].active
            and row["decision"].disposition in {"confirmed", "corrected"}
            and row["page_confirmed"] and row["processing_state"] == "completed"
            and page is not None and page.status == "extracted" and not row["open_issue"]
            and not row["ambiguous_field_mapping"]
        )
        if row["verified_position"]:
            selected_rows.append(row)

    workbook = Workbook()
    positions = workbook.active
    positions.title = "Проверенные позиции"
    source_locations: dict[str, tuple[str, int]] = {}
    # Keep each physical table on its own worksheet. Heterogeneous tables never
    # share a synthetic set of columns, and source identity is page/table/index.
    for page_number in selected:
        page = pages_by_number.get(page_number)
        if page is None:
            continue
        ordered_tables = sorted(page.tables, key=lambda table: (table.index, table.id))
        for table_ordinal, table in enumerate(ordered_tables, start=1):
            name = _source_sheet_name(page_number, table_ordinal)
            table_rows = sorted(
                (row for row in all_rows if not row["manual"] and row["table_id"] == table.id),
                key=lambda row: row["row_index"],
            )
            source_headers = {column.index: column.raw_header or "" for column in table.columns}
            # Preserve a cell even if a malformed/incomplete extraction omitted
            # its column descriptor; identify it by source index and blank header.
            for row in table_rows:
                for item in row["source_values"]:
                    source_headers.setdefault(int(item["column_index"]), "")
            columns = sorted(source_headers.items())
            locations = _source_table_sheet(
                workbook, name=name, title=f"Исходные и проверенные значения · страница {page_number}, таблица {table.index + 1} · ID {table.id}",
                filename=filename, document_id=draft.source_document_id, selected=selected, page_count=page_count,
                columns=columns, rows=table_rows,
            )
            source_locations.update({record_id: (name, row_number) for record_id, row_number in locations.items()})

    # Manual rows have their own per-page sheet, separate from model tables.
    for page_number in selected:
        manual_rows = [row for row in all_rows if row["manual"] and row["page"] == page_number]
        if not manual_rows:
            continue
        by_index: dict[int, str] = {}
        for row in manual_rows:
            for item in row["source_values"]:
                by_index.setdefault(int(item["column_index"]), str(item["header"] or ""))
        name = _source_sheet_name(page_number, "manual")
        locations = _source_table_sheet(
            workbook, name=name, title=f"Ручные строки · страница {page_number}",
            filename=filename, document_id=draft.source_document_id, selected=selected, page_count=page_count,
            columns=sorted(by_index.items()), rows=manual_rows,
        )
        source_locations.update({record_id: (name, row_number) for record_id, row_number in locations.items()})

    position_header = ["ТЕСТ", "Документ", "ID документа", "Страница", "Таблица", "Строка источника", "Статус проверки", "Роль", "Родитель компонента"]
    for field, label in CANONICAL_FIELDS:
        position_header.extend([f"{label} — исходно", f"{label} — проверено"])
    position_header.extend(["Лист исходной таблицы", "Строка на листе исходной таблицы", "ID строки"])
    position_rows = []
    for row in selected_rows:
        grouped = _values_by_field(row)
        values: list[object] = ["ТЕСТ", filename, draft.source_document_id, row["page"], row["table_index"], row["row_index"], row["review_status"], ROLE_LABELS.get(row["role"], row["role"]), row["parent_record_id"]]
        for field, _label in CANONICAL_FIELDS:
            mapped = grouped.get(field, [])
            if len(mapped) == 1:
                values.extend([mapped[0]["raw"], mapped[0]["current"]])
            else:
                values.extend(["" if not mapped else "Неоднозначное сопоставление", "" if not mapped else "Неоднозначное сопоставление"])
        source_sheet, source_row = source_locations.get(row["id"], ("", ""))
        values.extend([source_sheet, source_row, row["id"]])
        position_rows.append(values)
    _write_sheet(positions, "Подтверждённые самостоятельные позиции", filename, draft.source_document_id, selected, page_count, position_header, position_rows)

    all_sheet = workbook.create_sheet("Все распознанные строки")
    all_header = [
        "ТЕСТ", "Документ", "ID документа", "Страница", "Таблица", "Строка источника", "Тип по модели",
        "Роль Human Review", "Родитель компонента", "Статус проверки", "Статус обработки страницы",
        "Полнота страницы подтверждена", "Решение по значениям", "Лист исходной таблицы",
        "Строка на листе исходной таблицы", "Последние сохранённые значения Human Review (JSON)",
        "Последнее решение Human Review (JSON)", "Исходная строка и таблица (JSON)", "ID строки",
    ]
    all_data = []
    for row in all_rows:
        decision = row["decision"]
        source_sheet, source_row = source_locations.get(row["id"], ("", ""))
        all_data.append([
            "ТЕСТ", filename, draft.source_document_id, row["page"], row["table_index"], row["row_index"], row["model_kind"],
            ROLE_LABELS.get(row["role"], "Не определена") if row["role_active"] else "Не подтверждена человеком",
            row["parent_record_id"], row["review_status"], row["processing_state"],
            "Да" if row["page_confirmed"] else "Нет", DISPOSITION_LABELS.get(decision.disposition, "") if decision and decision.active else "Нет активного решения",
            source_sheet, source_row, _json_text(row["last_saved_values"]),
            _json_text(decision.model_dump(mode="json") if decision else {}),
            _json_text(row["evidence"]), row["id"],
        ])
    _write_sheet(all_sheet, "Все распознанные и добавленные строки выбранных страниц", filename, draft.source_document_id, selected, page_count, all_header, all_data)
    for sheet, source_column_name, row_column_name in (
        (positions, "Лист исходной таблицы", "Строка на листе исходной таблицы"),
        (all_sheet, "Лист исходной таблицы", "Строка на листе исходной таблицы"),
    ):
        headers = {sheet.cell(4, column).value: column for column in range(1, sheet.max_column + 1)}
        for row_number in range(5, sheet.max_row + 1):
            target_name = sheet.cell(row_number, headers[source_column_name]).value
            target_row = sheet.cell(row_number, headers[row_column_name]).value
            if target_name and target_row:
                link = sheet.cell(row_number, headers[source_column_name])
                link.hyperlink = f"#'{target_name}'!A{target_row}"
                link.style = "Hyperlink"

    coverage_sheet = workbook.create_sheet("Охват страниц")
    page_errors = _page_report_errors(report)
    coverage_header = ["Страница", "Статус обработки", "Безопасный код ошибки", "Результат извлечения", "Полнота подтверждена", "Строк в результате", "Проверенных самостоятельных позиций", "Неразрешённые замечания", "Охват документа"]
    coverage_data = []
    for page_number in selected:
        page = pages_by_number.get(page_number)
        page_rows = [row for row in all_rows if row["page"] == page_number]
        open_page_issues = [
            issue for issue in issue_index.get(page_number, [])
            if issue.severity in {"review", "blocking"} and issue.id not in review.resolved_issue_ids
        ]
        coverage_data.append([
            page_number, report_states.get(page_number, "не запускалась"), page_errors.get(page_number, ""), page.status if page else "нет результата",
            "Да" if page_number in confirmed_pages else "Нет", len(page_rows),
            sum(bool(row["verified_position"]) for row in page_rows), len(open_page_issues),
            "Полный" if set(selected) == expected else "Частичный",
        ])
    _write_sheet(coverage_sheet, "Состояние обработки выбранного фрагмента", filename, draft.source_document_id, selected, page_count, coverage_header, coverage_data)

    source_sheet = workbook.create_sheet("Исходная структура")
    source_header = ["Страница", "Часть", "Всего частей", "JSON исходного результата (объединить части по порядку без разделителей)"]
    source_rows = []
    for page_number in selected:
        page = pages_by_number.get(page_number)
        if page is None:
            continue
        encoded = _json_text(page.model_dump(mode="json"))
        chunks = [encoded[index:index + 30000] for index in range(0, len(encoded), 30000)] or [""]
        source_rows.extend([
            page_number, index + 1, len(chunks), chunk,
        ] for index, chunk in enumerate(chunks))
    _write_sheet(source_sheet, "Полная исходная структура выбранных страниц", filename, draft.source_document_id, selected, page_count, source_header, source_rows, widths=[12, 12, 14, 100])

    workbook.properties.title = "ТЕСТ — AI-first Qwen Vision"
    workbook.properties.subject = "Локальная пилотная выгрузка проверяемого фрагмента; не коммерческая смета"
    stream = BytesIO()
    try:
        workbook.save(stream)
    except (ValueError, OSError):
        raise PilotExportError("pilot_workbook_generation_failed") from None
    return stream.getvalue()


__all__ = ["MAX_PILOT_EXPORT_PAGES", "XLSX_MIME", "PilotExportError", "build_ai_first_pilot_xlsx"]
