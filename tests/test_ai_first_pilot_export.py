from __future__ import annotations

from io import BytesIO
import json

import pytest
from openpyxl import load_workbook

from averon_import.services.ai_first.contracts import (
    AICell, AIColumn, AIDocumentDraft, AIPageExtraction, AIRow, AITable,
)
from averon_import.services.ai_first.pilot_export import (
    PilotExportError, build_ai_first_pilot_xlsx,
)
from averon_import.services.ai_first.review import (
    confirm_page_complete, new_review_state, record_row_decision,
    record_row_role_classification, resolve_review_issue,
)


HEADERS = ["Поз.", "Наименование", "Марка", "Производитель", "Артикул", "Ед. изм.", "Количество", "Примечание"]


def _row(page_number: int, table_id: str, row_index: int, values: list[str], *, kind: str = "item") -> AIRow:
    record_id = f"p{page_number}-r{row_index}"
    cells = [AICell(
        id=f"{record_id}-c{index}", source_document_id="synthetic-doc",
        page_number=page_number, table_id=table_id, row_id=record_id,
        column_index=index, raw_value=value,
    ) for index, value in enumerate(values)]
    return AIRow(
        id=record_id, source_document_id="synthetic-doc", page_number=page_number,
        table_id=table_id, index=row_index, model_kind=kind, raw_cells=cells,
    )


def _fixture(*, issue: bool = False):
    table_id = "p1-t1"
    columns = [AIColumn(
        id=f"{table_id}-c{index}", source_document_id="synthetic-doc",
        page_number=1, table_id=table_id, index=index, raw_header=header,
    ) for index, header in enumerate(HEADERS)]
    parent = _row(1, table_id, 0, ["1", "Синтетический насос", "М-1", "Примерный завод", "SYN-1", "шт.", "2", "Исходная заметка"])
    section = _row(1, table_id, 1, ["", "Раздел В2", "", "", "", "", "", ""], kind="section")
    component = _row(1, table_id, 2, ["1.1", "Компонент комплекта", "М-2", "Завод 2", "SYN-2", "компл.", "1", "В составе комплекта"], kind="component")
    table = AITable(
        id=table_id, source_document_id="synthetic-doc", page_number=1,
        index=0, columns=columns, rows=[parent, section, component],
    )
    page = AIPageExtraction(
        source_document_id="synthetic-doc", page_number=1,
        extraction_fingerprint="page-fingerprint-1", status="extracted", tables=[table],
    )
    draft = AIDocumentDraft(
        source_document_id="synthetic-doc", revision_id="revision-1",
        pages=[page], expected_page_numbers=[1, 2],
    )
    review = new_review_state(draft)
    review = record_row_role_classification(draft, review, parent.id, role="standalone_item", reviewer_id="synthetic-reviewer")
    review = record_row_role_classification(draft, review, section.id, role="section", reviewer_id="synthetic-reviewer")
    review = record_row_role_classification(
        draft, review, component.id, role="component", parent_record_id=parent.id,
        reviewer_id="synthetic-reviewer",
    )
    review = record_row_decision(
        draft, review, parent.id, disposition="corrected", reviewer_id="synthetic-reviewer",
        values={"cell:6": "3"},
    )
    review = record_row_decision(
        draft, review, section.id, disposition="confirmed", reviewer_id="synthetic-reviewer",
    )
    review = record_row_decision(
        draft, review, component.id, disposition="confirmed", reviewer_id="synthetic-reviewer",
    )
    if issue:
        from averon_import.services.ai_first.contracts import AIReviewIssue
        draft = draft.model_copy(update={"issues": [AIReviewIssue(
            id="issue-section", source_document_id="synthetic-doc", code="synthetic_review",
            severity="review", page_number=1, record_ids=[parent.id], detail="synthetic fixture",
        )]})
    review = confirm_page_complete(draft, review, 1, reviewer_id="synthetic-reviewer")
    report = {"pages": [
        {"page_number": 1, "state": "completed"},
        {"page_number": 2, "state": "pending"},
    ]}
    return draft, review, report, parent, section, component


def _sheet(workbook_bytes: bytes, name: str):
    book = load_workbook(BytesIO(workbook_bytes), data_only=False)
    return book[name]


def test_pilot_xlsx_separates_verified_standalone_rows_and_preserves_all_source_data():
    draft, review, report, parent, section, component = _fixture()
    content = build_ai_first_pilot_xlsx(
        draft, review, report, page_numbers=[1], document_name="synthetic.pdf",
    )
    book = load_workbook(BytesIO(content), data_only=False)
    positions = book["Проверенные позиции"]
    all_rows = book["Все распознанные строки"]

    assert positions.max_row == 5  # three workbook headings plus one verified row
    headers = {positions.cell(4, column).value: column for column in range(1, positions.max_column + 1)}
    values = [positions.cell(5, column).value for column in range(1, positions.max_column + 1)]
    assert values[headers["Количество — исходно"] - 1] == "2"
    assert values[headers["Количество — проверено"] - 1] == "3"
    assert values[headers["Наименование — исходно"] - 1] == "Синтетический насос"
    assert values[headers["Наименование — проверено"] - 1] == "Синтетический насос"
    assert values[headers["Статус проверки"] - 1] == "Исправлена и подтверждена"
    assert positions["A1"].value.startswith("ТЕСТ —")
    assert "ЧАСТИЧНЫЙ ОХВАТ ДОКУМЕНТА" in positions["A2"].value

    all_headers = {all_rows.cell(4, column).value: column for column in range(1, all_rows.max_column + 1)}
    records = {
        all_rows.cell(row, all_headers["ID строки"]).value: row
        for row in range(5, all_rows.max_row + 1)
    }
    assert set(records) == {parent.id, section.id, component.id}
    component_row = records[component.id]
    assert all_rows.cell(component_row, all_headers["Роль Human Review"]).value == "Компонент комплекта"
    section_row = records[section.id]
    assert all_rows.cell(section_row, all_headers["Роль Human Review"]).value == "Раздел"
    source_detail = json.loads(all_rows.cell(records[parent.id], all_headers["Исходная строка и таблица (JSON)"]).value)
    source_values = json.loads(all_rows.cell(records[parent.id], all_headers["Исходные значения (JSON)"]).value)
    assert source_values[6]["value"] == "2"
    assert len(source_detail["row"]["raw_cell_ids"]) == len(HEADERS)
    assert source_detail["full_raw_payload_sheet"] == "Исходная структура"
    assert "Исходная структура" in book.sheetnames
    source_sheet = book["Исходная структура"]
    chunks = [source_sheet.cell(row, 4).value for row in range(5, source_sheet.max_row + 1)]
    assert json.loads("".join(chunks))["tables"][0]["rows"][0]["id"] == parent.id


def test_unclassified_rows_and_unresolved_issues_never_enter_verified_positions():
    draft, review, report, parent, section, component = _fixture(issue=True)
    # An unresolved row-level issue keeps even a manually confirmed row out.
    content = build_ai_first_pilot_xlsx(draft, review, report, page_numbers=[1], document_name="synthetic.pdf")
    book = load_workbook(BytesIO(content), data_only=False)
    assert book["Проверенные позиции"].max_row == 4
    all_rows = book["Все распознанные строки"]
    headers = {all_rows.cell(4, column).value: column for column in range(1, all_rows.max_column + 1)}
    parent_row = next(row for row in range(5, all_rows.max_row + 1) if all_rows.cell(row, headers["ID строки"]).value == parent.id)
    assert all_rows.cell(parent_row, headers["Статус проверки"]).value == "Есть неразрешённое замечание"


def test_resolved_row_level_visual_issue_can_be_human_verified():
    from averon_import.services.ai_first.contracts import AIReviewIssue

    draft, review, report, parent, *_ = _fixture()
    page = draft.pages[0]
    table = page.tables[0]
    row = table.rows[0]
    row.raw_cells[6] = row.raw_cells[6].model_copy(update={"struck_through":True})
    issue = AIReviewIssue(
        id="struck-quantity", source_document_id="synthetic-doc", code="row_has_uncertain_visual_evidence",
        severity="review", page_number=1, record_ids=[parent.id], detail="synthetic fixture",
    )
    page.issues.append(issue)
    draft = draft.model_copy(update={"issues":[issue]})
    review = resolve_review_issue(draft, review, issue.id, reviewer_id="synthetic-reviewer", detail="Сверено с PDF")
    content = build_ai_first_pilot_xlsx(draft, review, report, page_numbers=[1], document_name="synthetic.pdf")
    workbook = load_workbook(BytesIO(content), data_only=False)
    assert workbook["Проверенные позиции"].max_row == 5


def test_role_without_human_decision_is_exported_as_unverified_only():
    draft, review, report, parent, section, component = _fixture()
    review = review.model_copy(deep=True)
    review.decisions[parent.id] = review.decisions[parent.id].model_copy(update={"active": False})
    content = build_ai_first_pilot_xlsx(draft, review, report, page_numbers=[1], document_name="synthetic.pdf")
    book = load_workbook(BytesIO(content), data_only=False)
    assert book["Проверенные позиции"].max_row == 4
    all_rows = book["Все распознанные строки"]
    headers = {all_rows.cell(4, column).value: column for column in range(1, all_rows.max_column + 1)}
    parent_row = next(row for row in range(5, all_rows.max_row + 1) if all_rows.cell(row, headers["ID строки"]).value == parent.id)
    assert all_rows.cell(parent_row, headers["Статус проверки"]).value == "Значения требуют проверки"


def test_invalidated_role_change_keeps_saved_values_visible_but_not_verified():
    draft, review, report, parent, *_ = _fixture()
    review = review.model_copy(deep=True)
    review.decisions[parent.id] = review.decisions[parent.id].model_copy(update={
        "active": False, "invalidation_reason": "role_change",
    })
    content = build_ai_first_pilot_xlsx(draft, review, report, page_numbers=[1], document_name="synthetic.pdf")
    book = load_workbook(BytesIO(content), data_only=False)
    assert book["Проверенные позиции"].max_row == 4
    all_rows = book["Все распознанные строки"]
    headers = {all_rows.cell(4, column).value: column for column in range(1, all_rows.max_column + 1)}
    parent_row = next(row for row in range(5, all_rows.max_row + 1) if all_rows.cell(row, headers["ID строки"]).value == parent.id)
    current = json.loads(all_rows.cell(parent_row, headers["Значения после проверки (JSON)"]).value)
    saved = json.loads(all_rows.cell(parent_row, headers["Последние сохранённые значения Human Review (JSON)"]).value)
    assert current[6]["value"] == "2"
    assert saved["cell:6"] == "3"
    assert json.loads(all_rows.cell(parent_row, headers["Последнее решение Human Review (JSON)"]).value)["invalidation_reason"] == "role_change"


def test_export_is_scoped_to_at_most_three_real_document_pages():
    draft, review, report, *_ = _fixture()
    with pytest.raises(PilotExportError, match="invalid_page_scope"):
        build_ai_first_pilot_xlsx(draft, review, report, page_numbers=[], document_name="synthetic.pdf")
    with pytest.raises(PilotExportError, match="invalid_page_scope"):
        build_ai_first_pilot_xlsx(draft, review, report, page_numbers=[1, 1], document_name="synthetic.pdf")
    with pytest.raises(PilotExportError, match="page_outside_document"):
        build_ai_first_pilot_xlsx(draft, review, report, page_numbers=[3], document_name="synthetic.pdf")


def test_selected_page_export_does_not_require_all_28_document_pages():
    draft, review, _report, *_ = _fixture()
    draft = draft.model_copy(update={"expected_page_numbers": list(range(1, 29))})
    report = {"pages": [
        {"page_number": 1, "state": "completed"},
        *[{"page_number": page, "state": "pending"} for page in range(2, 29)],
    ]}

    content = build_ai_first_pilot_xlsx(
        draft, review, report, page_numbers=[1], document_name="synthetic-28-pages.pdf",
    )
    workbook = load_workbook(BytesIO(content), data_only=False)
    positions = workbook["Проверенные позиции"]
    coverage = workbook["Охват страниц"]
    assert positions.max_row == 5
    assert "ЧАСТИЧНЫЙ ОХВАТ ДОКУМЕНТА" in positions["A2"].value
    assert coverage.max_row == 5
    assert coverage.cell(5, 1).value == "1"


def test_ambiguous_duplicate_column_headers_do_not_enter_verified_positions():
    draft, review, report, parent, *_ = _fixture()
    table = draft.pages[0].tables[0]
    table.columns[7] = table.columns[7].model_copy(update={"raw_header":"Количество"})
    content = build_ai_first_pilot_xlsx(draft, review, report, page_numbers=[1], document_name="synthetic.pdf")
    book = load_workbook(BytesIO(content), data_only=False)
    assert book["Проверенные позиции"].max_row == 4
    all_rows = book["Все распознанные строки"]
    headers = {all_rows.cell(4, column).value: column for column in range(1, all_rows.max_column + 1)}
    parent_row = next(row for row in range(5, all_rows.max_row + 1) if all_rows.cell(row, headers["ID строки"]).value == parent.id)
    assert all_rows.cell(parent_row, headers["Статус проверки"]).value == "Неоднозначное сопоставление колонок"


def test_ocr_values_are_written_as_text_and_not_excel_formulas():
    draft, review, report, parent, *_ = _fixture()
    row = draft.pages[0].tables[0].rows[0]
    row.raw_cells[1] = row.raw_cells[1].model_copy(update={"raw_value": "=HYPERLINK(\"https://example.invalid\",\"click\")"})
    content = build_ai_first_pilot_xlsx(draft, review, report, page_numbers=[1], document_name="synthetic.pdf")
    book = load_workbook(BytesIO(content), data_only=False)
    all_rows = book["Все распознанные строки"]
    headers = {all_rows.cell(4, column).value: column for column in range(1, all_rows.max_column + 1)}
    values = json.loads(all_rows.cell(5, headers["Исходные значения (JSON)"]).value)
    assert values[1]["value"].startswith("=HYPERLINK")
    assert all_rows.cell(5, headers["Исходные значения (JSON)"]).data_type == "s"

    positions = book["Проверенные позиции"]
    position_headers = {positions.cell(4, column).value: column for column in range(1, positions.max_column + 1)}
    name_column = position_headers["Наименование — исходно"]
    assert positions.cell(5, name_column).value.startswith("=HYPERLINK")
    assert positions.cell(5, name_column).data_type == "s"


def test_export_refuses_excel_cell_overflow_without_truncating_source_text():
    draft, review, report, *_ = _fixture()
    row = draft.pages[0].tables[0].rows[0]
    row.raw_cells[1] = row.raw_cells[1].model_copy(update={"raw_value":"x" * 32768})
    with pytest.raises(PilotExportError, match="export_cell_limit_exceeded"):
        build_ai_first_pilot_xlsx(draft, review, report, page_numbers=[1], document_name="synthetic.pdf")


def test_large_model_payload_is_chunked_without_breaking_row_review_export():
    draft, review, report, *_ = _fixture()
    table = draft.pages[0].tables[0]
    table.raw_model_payload = {"synthetic_payload": "x" * 100_000}

    content = build_ai_first_pilot_xlsx(draft, review, report, page_numbers=[1], document_name="synthetic.pdf")
    workbook = load_workbook(BytesIO(content), data_only=False)
    all_rows = workbook["Все распознанные строки"]
    headers = {all_rows.cell(4, column).value: column for column in range(1, all_rows.max_column + 1)}
    row_evidence = json.loads(all_rows.cell(5, headers["Исходная строка и таблица (JSON)"]).value)
    assert row_evidence["full_raw_payload_sheet"] == "Исходная структура"

    source = workbook["Исходная структура"]
    chunks = [source.cell(row, 4).value for row in range(5, source.max_row + 1)]
    assert all(len(chunk) <= 30000 for chunk in chunks)
    assert "synthetic_payload" in "".join(chunks)
